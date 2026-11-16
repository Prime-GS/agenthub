"""Everything that touches the machine: names, /proc, cgroupfs, systemctl, inotify, sd_notify."""
import ctypes
import os
import re
import shutil
import socket
import struct
import subprocess

PREFIX = re.compile(r'^[a-z][a-z0-9]*$')
CGROUP_ROOT = '/sys/fs/cgroup'
SYSTEMCTL_TIMEOUT = 10


def prefix():
    """Unit and path prefix. No dashes: systemd reads a dash in a slice name as one level of hierarchy."""
    value = os.environ.get('AGENTHUB_PREFIX', 'agenthub')
    if not PREFIX.match(value):
        raise ValueError(f'AGENTHUB_PREFIX must match {PREFIX.pattern}, got {value!r}')
    return value


def runtime_dir():
    return os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'


def socket_path(pfx):
    return os.environ.get('AGENTHUB_SOCKET') or os.path.join(runtime_dir(), f'{pfx}.sock')


def state_dir(pfx):
    return os.path.join(runtime_dir(), pfx)


def root_slice(pfx):
    return f'{pfx}.slice'


def class_slice(pfx, cls):
    return f'{pfx}-{cls}.slice'


def run_unit(pfx, rid):
    return f'{pfx}-run-{rid}.scope'


def direct_unit(pfx):
    return f'{pfx}-direct-{os.getpid()}.scope'


# memory and cgroups

def meminfo():
    out = {}
    with open('/proc/meminfo') as f:
        for line in f:
            key, _, rest = line.partition(':')
            out[key] = int(rest.split()[0]) * 1024
    return out


def manager_cgroup():
    """cgroup path of the user's systemd manager (…/user@UID.service), taken from this process's own cgroup."""
    try:
        with open('/proc/self/cgroup') as f:
            path = f.read().strip().split('::', 1)[-1]
        m = re.match(r'^(.*/user@\d+\.service)(/|$)', path)
        if m:
            return m.group(1)
    except OSError:
        pass
    uid = os.getuid()
    return f'/user.slice/user-{uid}.slice/user@{uid}.service'


class Cgroups:
    def __init__(self, pfx, base=None):
        self.pfx = pfx
        self.base = os.path.join(CGROUP_ROOT, (base or manager_cgroup()).lstrip('/'), root_slice(pfx))

    def class_dir(self, cls):
        return os.path.join(self.base, class_slice(self.pfx, cls))

    def run_dir(self, cls, rid):
        return os.path.join(self.class_dir(cls), run_unit(self.pfx, rid))

    def scopes(self):
        """(class, run id, directory) for every run scope that exists now."""
        found = []
        pattern = re.compile(rf'^{self.pfx}-run-(\d+)\.scope$')
        try:
            slices = os.listdir(self.base)
        except OSError:
            return found
        for name in slices:
            m = re.match(rf'^{self.pfx}-(p\d)\.slice$', name)
            if not m:
                continue
            directory = os.path.join(self.base, name)
            for scope in os.listdir(directory):
                s = pattern.match(scope)
                if s:
                    found.append((m.group(1), int(s.group(1)), os.path.join(directory, scope)))
        return found


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_keyed(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    out[parts[0]] = int(parts[1])
    except (OSError, ValueError):
        pass
    return out


def populated(directory):
    """True while the cgroup has processes; False when it is empty or gone."""
    return read_keyed(os.path.join(directory, 'cgroup.events')).get('populated') == 1


def memory_current(directory):
    return read_int(os.path.join(directory, 'memory.current'))


def memory_peak(directory):
    return read_int(os.path.join(directory, 'memory.peak'))


def cpu_usage_usec(directory):
    return read_keyed(os.path.join(directory, 'cpu.stat')).get('usage_usec')


def pressure(resource='memory'):
    out = {}
    try:
        with open(f'/proc/pressure/{resource}') as f:
            for line in f:
                kind, *fields = line.split()
                out[kind] = {k: float(v) for k, v in (x.split('=') for x in fields)}
    except OSError:
        pass
    return out


# systemd

def systemctl(*args, check=False):
    # without NOTIFY_SOCKET: messages from a child of the hub are rejected by systemd and only add journal noise
    env = {k: v for k, v in os.environ.items() if k != 'NOTIFY_SOCKET'}
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True,
                          timeout=SYSTEMCTL_TIMEOUT, check=check, env=env)


def stop_unit(unit):
    """Asks systemd to stop a unit; False when systemctl is missing or does not answer."""
    try:
        return systemctl('stop', '--no-block', unit).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def can_make_scopes():
    """`systemd-run --user --scope` works only with the binary and a reachable user manager."""
    return shutil.which('systemd-run') is not None and os.path.exists(os.path.join(runtime_dir(), 'systemd', 'private'))


def scope_argv(unit, slice_name, memory_high, argv):
    return ['systemd-run', '--user', '--scope', '--quiet', '--collect', f'--unit={unit}', f'--slice={slice_name}',
            '-p', f'MemoryHigh={memory_high}', '--', *argv]


def in_unit(pid, unit):
    try:
        with open(f'/proc/{pid}/cgroup') as f:
            return f.read().rstrip().endswith('/' + unit)
    except OSError:
        return False


def notify(message):
    """sd_notify: a datagram to $NOTIFY_SOCKET; silently nothing outside systemd."""
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
        try:
            s.connect(address)
            s.sendall(message.encode())
        except OSError:
            pass


def activated_socket():
    """The listening socket systemd passed (socket activation), or None."""
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or int(os.environ.get('LISTEN_FDS', '0')) < 1:
        return None
    for key in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
        os.environ.pop(key, None)
    return socket.socket(fileno=3)


def peer_uid(sock):
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', data)[1]


# inotify (no binding in the standard library; three libc calls)

IN_MODIFY = 0x00000002
IN_NONBLOCK = os.O_NONBLOCK
IN_CLOEXEC = os.O_CLOEXEC
EVENT = struct.Struct('iIII')


class Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_init1 failed')

    def watch(self, path):
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), IN_MODIFY)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f'inotify_add_watch failed for {path}')
        return wd

    def unwatch(self, wd):
        self.libc.inotify_rm_watch(self.fd, wd)

    def read(self):
        """Watch descriptors that fired since the last read."""
        try:
            data = os.read(self.fd, 64 * 1024)
        except BlockingIOError:
            return []
        fired, offset = [], 0
        while offset + EVENT.size <= len(data):
            wd, _mask, _cookie, length = EVENT.unpack_from(data, offset)
            fired.append(wd)
            offset += EVENT.size + length
        return fired

    def close(self):
        os.close(self.fd)
