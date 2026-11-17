"""Optional root extras: disk weights for the agent slices and memory protection for the desktop.

agenthub never runs anything as root. `agenthub root-extras` prints a shell script for the machine's owner to
read and run; `agenthub install` says which parts are in place. What each part needs:

- disk weights (the `IOWeight=` of the agent slices): the io controller delegated to the user manager (a drop-in
  for `user@.service`, which counts once the user manager starts again), and a disk that honours weights — the
  BFQ scheduler, or the kernel's iocost controller switched on for that disk (NVMe disks usually run with no
  scheduler at all);
- desktop memory protection (the `MemoryLow=` of `app.slice` and `session.slice`, which `agenthub install`
  writes): a cgroup keeps `memory.low` only as far as its parents have it, so every cgroup from `user.slice` down
  to the user manager needs it too.
"""
import os
import re
import subprocess
import sys
import textwrap

from . import config as configmod
from . import system

SYS = '/sys'
IO_DROPIN = '50-agenthub-io.conf'
# its own name: an instance drop-in (user@UID.service.d) hides a template one (user@.service.d) of the same name
MEMORY_DROPIN = '50-agenthub-memory.conf'
TMPFILES = 'tmpfiles.d/agenthub-iocost.conf'
DESKTOP = ('app.slice', 'session.slice')   # the user manager's slices for applications and the session itself
DEV = re.compile(r'\d+:\d+')
NAME = re.compile(r'[\w.-]+')
UNIT = re.compile(r'[\w@.-]+\.(slice|service)')
RESTART = 'once the user manager starts again (log out of every session, or reboot)'


def read(path, default=''):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def manager_cgroup():
    """The user manager's cgroup as the manager itself reports it (right from an ssh or console login too)."""
    try:
        out = subprocess.run(['systemctl', '--user', 'show', '-p', 'ControlGroup', '--value'], capture_output=True,
                             text=True, timeout=system.SYSTEMCTL_TIMEOUT).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ''
    return out or system.manager_cgroup()


def manager_units():
    """The units from `user.slice` down to this user's manager, outermost first; [] when the cgroup path is not
    that plain layout (a container, a nested manager)."""
    units = [u for u in manager_cgroup().split('/') if u]
    return units if units and all(UNIT.fullmatch(u) for u in units) else []


def unit_delegates(uid):
    """The controllers the user@UID.service unit delegates, as the system manager has it loaded now."""
    try:
        return subprocess.run(['systemctl', 'show', f'user@{uid}.service', '-p', 'DelegateControllers', '--value'],
                              capture_output=True, text=True, timeout=system.SYSTEMCTL_TIMEOUT).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


def io_state(uid, units):
    """'on' (the user manager hands io to its slices), 'pending' (delegated, not in use yet), 'off' or 'unknown'."""
    if not units:
        return 'unknown'
    manager = os.path.join(system.CGROUP_ROOT, *units)
    if 'io' in read(os.path.join(manager, 'cgroup.subtree_control')).split():
        return 'on'
    if 'io' in read(os.path.join(manager, 'cgroup.controllers')).split() or 'io' in unit_delegates(uid):
        return 'pending'
    return 'off'


def disk_of(path):
    """(name, 'major:minor') of the whole disk holding `path`, through a partition; None when that cannot be told:
    device-mapper and RAID (LVM, LUKS, md), or no block device of its own (btrfs, network, tmpfs)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    block = os.path.realpath(os.path.join(SYS, 'dev', 'block', f'{os.major(st.st_dev)}:{os.minor(st.st_dev)}'))
    if os.path.exists(os.path.join(block, 'partition')):
        block = os.path.dirname(block)
    name, dev = os.path.basename(block), read(os.path.join(block, 'dev'))
    try:
        layered = bool(os.listdir(os.path.join(block, 'slaves')))
    except OSError:
        layered = False
    if layered or not DEV.fullmatch(dev) or not NAME.fullmatch(name) or not os.path.isdir(os.path.join(block, 'queue')):
        return None
    return name, dev


def scheduler(name):
    """The active I/O scheduler of a disk (the one in brackets), or None."""
    m = re.search(r'\[([\w-]+)\]', read(os.path.join(SYS, 'block', name, 'queue', 'scheduler')))
    return m.group(1) if m else None


def iocost_on(dev):
    for line in read(os.path.join(system.CGROUP_ROOT, 'io.cost.qos')).splitlines():
        words = line.split()
        if words[:1] == [dev] and 'enable=1' in words:
            return True
    return False


def low(path):
    value = read(os.path.join(path, 'memory.low'), '0')
    return 1 << 62 if value == 'max' else int(value) if value.isdigit() else 0


def protection(units):
    """What the desktop can keep, in bytes: the smallest memory.low from user.slice down to the user manager, and
    of the desktop slices under it."""
    path, lows = system.CGROUP_ROOT, []
    for unit in units:
        path = os.path.join(path, unit)
        lows.append(low(path))
    if units:
        lows += [low(os.path.join(path, s)) for s in DESKTOP]
    return min(lows, default=0)


def state(uid):
    units = manager_units()
    disk = disk_of(os.path.expanduser('~'))
    return {'io': io_state(uid, units), 'disk': disk, 'scheduler': scheduler(disk[0]) if disk else None,
            'iocost': bool(disk) and iocost_on(disk[1]), 'protected': protection(units), 'units': units}


def describe(s):
    """How the disk of the home directory treats weights, in words."""
    if s['disk'] is None:
        return 'the disk of the home directory could not be told (LVM, LUKS, RAID, btrfs)'
    if s['scheduler'] == 'bfq':
        return f'{s["disk"][0]}: BFQ scheduler'
    if s['iocost']:
        return f'{s["disk"][0]}: iocost on'
    sched = 'no I/O scheduler' if s['scheduler'] in (None, 'none') else f'the {s["scheduler"]} scheduler'
    return f'{s["disk"][0]}: {sched} and iocost off, so weights do not count'


def report(cfg, uid=None):
    """Lines for `agenthub install`: each part on, pending, off or unknown, and why. Never "on" from configuration
    alone: only from what the kernel shows in use."""
    s = state(os.getuid() if uid is None else uid)
    io = {'on': 'io in use by the user manager', 'pending': f'io delegated, in use {RESTART}',
          'off': 'io not delegated to the user manager',
          'unknown': f'the user manager\'s cgroup ({manager_cgroup()}) is not the usual layout'}[s['io']]
    honours = None if s['disk'] is None else s['scheduler'] == 'bfq' or s['iocost']
    if s['io'] == 'off' or honours is False:
        weights = 'off'
    elif s['io'] == 'unknown' or honours is None:
        weights = 'unknown'
    else:
        weights = 'on' if s['io'] == 'on' else 'pending'
    reserve = cfg.desktop_reserve
    if not reserve:
        memory, why = 'off', 'desktop_reserve is 0'
    elif not s['units']:
        memory, why = 'unknown', 'the user manager\'s cgroup is not the usual layout'
    else:
        memory = 'on' if s['protected'] >= reserve else 'off'
        why = (f'{", ".join([*s["units"], *DESKTOP])} protect at least {configmod.format_size(s["protected"])}; '
               f'the desktop reserve is {configmod.format_size(reserve)}')
    lines = [f'root extras: disk weights {weights} ({io}; {describe(s)})',
             f'root extras: desktop memory protection {memory} ({why})']
    if weights in ('off', 'unknown') or memory == 'off' and reserve:
        lines.append('root extras are optional; `agenthub root-extras > agenthub-root.sh` writes the script that '
                     'adds them: read it, then run `sudo sh agenthub-root.sh`')
    return lines


def comment(text):
    return textwrap.wrap(text, 108, initial_indent='# ', subsequent_indent='#    ')


def installed(etc='/etc'):
    """Whether files of the root script are in place (for `agenthub uninstall` to point at `--undo`)."""
    return any(os.path.exists(os.path.join(etc, p)) for p in
               (os.path.join('systemd/system/user@.service.d', IO_DROPIN),
                os.path.join('systemd/system/user.slice.d', MEMORY_DROPIN), TMPFILES))


def script(cfg, undo=False, uid=None, etc='/etc'):
    """A shell script for root that adds the extras, or with `undo` removes them. Only printed, never run here."""
    uid = os.getuid() if uid is None else uid
    s = state(uid)
    units, qos = s['units'], os.path.join(system.CGROUP_ROOT, 'io.cost.qos')
    if not units:
        raise RuntimeError(f'the user manager\'s cgroup ({manager_cgroup()}) is not the usual '
                           'user.slice/user-UID.slice/user@UID.service: set the extras up by hand (docs/design.md §2.3)')
    tmpfiles = os.path.join(etc, TMPFILES)
    io_dir = os.path.join(etc, 'systemd/system/user@.service.d')
    memory = [(os.path.join(etc, 'systemd/system', f'{u}.d'), 'Service' if u.endswith('.service') else 'Slice')
              for u in units]
    lines = ['#!/bin/sh',
             '# agenthub root extras' + (': removal' if undo else
                                          ': weights for agent disk work, memory protection for the desktop'),
             f'# printed by `agenthub root-extras{" --undo" if undo else ""}` for {", ".join(units)}; agenthub '
             'never runs it itself',
             'set -eu',
             '[ "$(id -u)" = 0 ] || { echo "agenthub: run this as root" >&2; exit 1; }']
    if undo:
        lines += ['rm -f ' + ' '.join([os.path.join(io_dir, IO_DROPIN),
                                       *(os.path.join(d, MEMORY_DROPIN) for d, _ in memory)]),
                  f'if [ -f {tmpfiles} ]; then',
                  f'    dev=$(awk \'$1 == "w" {{ print $7; exit }}\' {tmpfiles})',
                  f'    rm -f {tmpfiles}',
                  '    # the disk may have another number by now: a refused write only leaves iocost on until reboot',
                  f'    [ -z "$dev" ] || echo "$dev enable=0" > {qos} || echo "agenthub: iocost stays on for $dev '
                  'until reboot" >&2',
                  'fi',
                  f'for d in {io_dir} {" ".join(d for d, _ in memory)}; do rmdir "$d" 2>/dev/null || true; done',
                  'systemctl daemon-reload',
                  f'echo "agenthub: root extras removed; the io controller stays delegated until the user manager '
                  'starts again"']
        return '\n'.join(lines) + '\n'
    done = ['disk weights ' + RESTART]
    lines += ['umask 022', '']
    lines += comment('1. The io controller for user managers, so that the IOWeight= of the agent slices counts. '
                     'Delegate= lists add up: this adds io to what user@.service delegates already. For every user, '
                     f'{RESTART}.')
    lines += [f'mkdir -p {io_dir}', f"printf '[Service]\\nDelegate=io\\n' > {os.path.join(io_dir, IO_DROPIN)}", '']
    disk, sched = s['disk'], s['scheduler']
    if disk is None:
        done = ['disk weights only once the disk honours them (see 2.)']
        lines += comment('2. The disk of the home directory could not be told (LVM, LUKS, RAID, btrfs). Weights count '
                         f'with the BFQ scheduler, or with iocost: echo "MAJ:MIN enable=1" > {qos} for the disk '
                         'under it.')
    elif sched == 'bfq':
        lines += comment(f'2. {disk[0]} runs the BFQ scheduler, which honours weights: nothing to do.')
    elif s['iocost']:
        lines += comment(f'2. iocost is on for {disk[0]} already: nothing to do.')
    elif not os.path.exists(qos):
        done = ['disk weights only once the disk honours them (see 2.)']
        lines += comment(f'2. {describe(s)}, and this kernel has no iocost controller: switch the disk to the BFQ '
                         'scheduler (a udev rule) for weights to count.')
    else:
        lines += comment(f'2. {describe(s)}. The kernel\'s iocost controller makes them count: switched on now, and at '
                         f'every boot by tmpfiles if the kernel takes it. {disk[1]} is the disk\'s number now: after '
                         f'adding or reordering disks, check {tmpfiles}.')
        lines += [f'if echo "{disk[1]} enable=1" > {qos}; then',
                  f'    mkdir -p {os.path.dirname(tmpfiles)}',
                  f"    printf 'w {qos} - - - - {disk[1]} enable=1\\n' > {tmpfiles}",
                  'else',
                  f'    echo "agenthub: the kernel refused iocost for {disk[0]}; disk weights will not count" >&2',
                  'fi']
    lines.append('')
    reserve = int(cfg.desktop_reserve)
    if reserve:
        done.insert(0, 'memory protection now')
        lines += comment(f'3. Memory protection for the desktop: MemoryLow={reserve} '
                         f'({configmod.format_size(reserve)}, the desktop reserve) on every cgroup above it, since a '
                         'cgroup keeps memory.low only as far as its parents have it. user.slice holds the sessions '
                         'of all users together.')
        for directory, section in memory:
            lines += [f'mkdir -p {directory}',
                      f"printf '[{section}]\\nMemoryLow={reserve}\\n' > {os.path.join(directory, MEMORY_DROPIN)}"]
    else:
        lines += comment('3. desktop_reserve is 0: no memory protection.')
    lines += ['', 'systemctl daemon-reload', f'echo "agenthub: done; {", ".join(done)}"']
    return '\n'.join(lines) + '\n'


def cmd_root_extras(args):
    if os.getuid() == 0:   # root's home, config and manager are not the ones the script is for
        print('agenthub: run `agenthub root-extras` as your own user; only its output runs as root', file=sys.stderr)
        return 2
    try:
        text = script(configmod.load(), undo=args.undo)
    except (configmod.ConfigError, RuntimeError, ValueError) as e:
        print(f'agenthub: {e}', file=sys.stderr)
        return 1
    print(text, end='')
    return 0
