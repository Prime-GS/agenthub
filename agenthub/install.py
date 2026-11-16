"""Installs the systemd user units (slices, socket, service) and the `agenthub` command, and removes them."""
import glob
import os
import shlex
import shutil
import sys

from . import config as configmod
from . import system
from .config import CLASSES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def unit_dir(runtime):
    if runtime:
        return os.path.join(system.runtime_dir(), 'systemd', 'user')
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'systemd', 'user')


def unit_quote(text):
    """One word for a unit file: quoted, and `%` kept literal instead of a systemd specifier."""
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def memory_max(cfg):
    """Hard limit of the agent slice: budget + 10 %, kept 512 MiB under the RAM, never below the budget."""
    return max(cfg.agent_budget, min(int(cfg.agent_budget * 1.1), cfg.total_ram - (512 << 20)))


def units(pfx, cfg, python=sys.executable, root=ROOT):
    """{file name: content} for every unit agenthub needs; numbers come from the config at install time."""
    out = {
        system.root_slice(pfx): (
            '[Unit]\nDescription=agenthub: agent work\n\n'
            f'[Slice]\nCPUWeight={cfg.cpu_weight}\nMemoryHigh={cfg.agent_budget}\n'
            f'MemoryMax={memory_max(cfg)}\n'),
    }
    for cls in CLASSES:
        spec = cfg.classes[cls]
        low = f'MemoryLow={spec.memory_low}\n' if spec.memory_low else ''
        out[system.class_slice(pfx, cls)] = (
            f'[Unit]\nDescription=agenthub: class {cls}\n\n[Slice]\nCPUWeight={spec.cpu_weight}\n{low}')
    env = f'Environment={unit_quote("PYTHONPATH=" + root)} AGENTHUB_PREFIX={pfx}\n'
    if os.environ.get('AGENTHUB_CONFIG'):
        env += f'Environment={unit_quote("AGENTHUB_CONFIG=" + os.environ["AGENTHUB_CONFIG"])}\n'
    out[f'{pfx}.socket'] = (
        '[Unit]\nDescription=agenthub socket\n\n'
        f'[Socket]\nListenStream=%t/{pfx}.sock\nSocketMode=0600\nRemoveOnStop=yes\n\n[Install]\nWantedBy=sockets.target\n')
    out[f'{pfx}.service'] = (
        f'[Unit]\nDescription=agenthub: load hub for agent sessions\nRequires={pfx}.socket\nAfter={pfx}.socket\n\n'
        f'[Service]\nType=notify\nNotifyAccess=main\nExecStart={unit_quote(python)} -P -m agenthub hub\n{env}'
        'WatchdogSec=30\nRestart=on-failure\nRestartSec=1\nRestartPreventExitStatus=78\n'
        'MemoryMax=256M\nCPUWeight=200\n')
    return out


def wrapper_path():
    return os.path.expanduser('~/.local/bin/agenthub')


def install(runtime=False):
    pfx = system.prefix()
    cfg = configmod.load()
    directory = unit_dir(runtime)
    os.makedirs(directory, exist_ok=True)
    for name, text in units(pfx, cfg).items():
        with open(os.path.join(directory, name), 'w') as f:
            f.write(text)
    if not runtime:
        path = wrapper_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(f'#!/bin/sh\nPYTHONPATH={shlex.quote(ROOT)} exec {shlex.quote(sys.executable)} -P -m agenthub "$@"\n')
        os.chmod(path, 0o755)
    system.systemctl('daemon-reload', check=True)
    system.systemctl('enable' if not runtime else 'start', *(['--now'] if not runtime else []), f'{pfx}.socket',
                     check=True)
    print(f'installed {len(units(pfx, cfg))} units in {directory}; socket {system.socket_path(pfx)}')
    print(f'agent budget {configmod.format_size(cfg.agent_budget)}, desktop reserve '
          f'{configmod.format_size(cfg.desktop_reserve)} (config: {cfg.source})')
    return 0


def uninstall(runtime=False):
    pfx = system.prefix()
    directory = unit_dir(runtime)
    system.systemctl('stop', f'{pfx}.socket', f'{pfx}.service')
    if not runtime:
        system.systemctl('disable', f'{pfx}.socket')
    removed = 0
    for name in [system.root_slice(pfx), *(system.class_slice(pfx, c) for c in CLASSES), f'{pfx}.socket',
                 f'{pfx}.service']:
        try:
            os.unlink(os.path.join(directory, name))
            removed += 1
        except FileNotFoundError:
            pass
    if not runtime and os.path.exists(wrapper_path()):
        os.unlink(wrapper_path())
    shutil.rmtree(system.state_dir(pfx), ignore_errors=True)
    system.systemctl('daemon-reload')
    system.systemctl('reset-failed', f'{pfx}.socket', f'{pfx}.service')
    base = system.Cgroups(pfx).base
    # run scopes and fail-open `-direct-` scopes alike: stopping a slice would kill them
    running = sorted(os.path.basename(d) for d in glob.glob(os.path.join(base, '*.slice', '*.scope'))
                     if system.populated(d))
    if running:
        print(f'removed {removed} units from {directory}; {", ".join(running)} keep going, '
              'their slices go when they end')
    else:
        # stopping a slice stops everything in it, so only once nothing runs there
        system.systemctl('stop', *(system.class_slice(pfx, c) for c in CLASSES), system.root_slice(pfx))
        print(f'removed {removed} units from {directory}')
    return 0
