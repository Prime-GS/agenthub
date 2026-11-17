"""Root extras on a fake /sys and cgroup tree: what `agenthub install` reports, and the root script run for real
with `id` and `systemctl` stubbed and /etc moved into a temp directory."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from agenthub import config as c
from agenthub import extras, install, system

GIB = 1 << 30
MANAGER = '/user.slice/user-1000.slice/user@1000.service'
UNITS = ['user.slice', 'user-1000.slice', 'user@1000.service']


class Machine:
    """A temp directory with sysfs (the disk under a given path) and cgroupfs pieces, patched in."""

    def __init__(self, test, scheduler='[none] mq-deadline', qos='', controllers='cpu memory pids', subtree='',
                 lows=(0, 0, 0), desktop_low=0, layered=False, unit_delegates='cpu memory pids', manager=MANAGER):
        self.dir = tempfile.mkdtemp(prefix='agenthub-extras-')
        test.addCleanup(shutil.rmtree, self.dir)
        self.sys, self.cg = os.path.join(self.dir, 'sys'), os.path.join(self.dir, 'cg')
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.home)
        disk = os.path.join(self.sys, 'devices', 'nvme0n1')
        os.makedirs(os.path.join(disk, 'nvme0n1p5'))
        os.makedirs(os.path.join(disk, 'queue'))
        os.makedirs(os.path.join(disk, 'slaves'))
        if layered:
            os.makedirs(os.path.join(disk, 'slaves', 'sda1'))
        for path, text in ((os.path.join(disk, 'dev'), '259:0'), (os.path.join(disk, 'nvme0n1p5', 'dev'), '259:5'),
                           (os.path.join(disk, 'nvme0n1p5', 'partition'), '5'),
                           (os.path.join(disk, 'queue', 'scheduler'), scheduler)):
            self.write(path, text)
        st = os.stat(self.home)
        os.makedirs(os.path.join(self.sys, 'dev', 'block'))
        os.symlink(os.path.join(disk, 'nvme0n1p5'),
                   os.path.join(self.sys, 'dev', 'block', f'{os.major(st.st_dev)}:{os.minor(st.st_dev)}'))
        os.makedirs(os.path.join(self.sys, 'block'))
        os.symlink(disk, os.path.join(self.sys, 'block', 'nvme0n1'))
        if qos is not None:
            self.write(os.path.join(self.cg, 'io.cost.qos'), qos)
        path = self.cg
        for unit, low in zip(UNITS, lows):
            path = os.path.join(path, unit)
            self.write(os.path.join(path, 'memory.low'), str(low))
        self.write(os.path.join(path, 'cgroup.controllers'), controllers)
        self.write(os.path.join(path, 'cgroup.subtree_control'), subtree)
        for unit in extras.DESKTOP:
            self.write(os.path.join(path, unit, 'memory.low'), str(desktop_low))
        for patch in (mock.patch.object(extras, 'SYS', self.sys), mock.patch.object(system, 'CGROUP_ROOT', self.cg),
                      mock.patch.object(extras, 'manager_cgroup', return_value=manager),
                      mock.patch.object(extras, 'unit_delegates', return_value=unit_delegates.split()),
                      mock.patch.dict(os.environ, {'HOME': self.home})):
            patch.start()
            test.addCleanup(patch.stop)

    @staticmethod
    def write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)


def cfg(reserve='4G'):
    return c.build({'machine': {'desktop_reserve': reserve}}, 16 * GIB)


class StateTest(unittest.TestCase):
    def test_the_disk_is_the_whole_disk_under_the_partition(self):
        Machine(self)
        self.assertEqual(extras.disk_of(os.path.expanduser('~')), ('nvme0n1', '259:0'))
        self.assertEqual(extras.scheduler('nvme0n1'), 'none')
        self.assertIsNone(extras.disk_of('/nowhere'))

    def test_layers_and_missing_devices_are_unknown(self):
        Machine(self, layered=True)
        self.assertIsNone(extras.disk_of(os.path.expanduser('~')))
        m = Machine(self)
        os.unlink(os.path.join(m.sys, 'dev', 'block', os.listdir(os.path.join(m.sys, 'dev', 'block'))[0]))
        self.assertIsNone(extras.disk_of(os.path.expanduser('~')))

    def test_io_in_use_pending_or_off(self):
        Machine(self, controllers='cpu io memory pids', subtree='cpu io memory pids')
        self.assertEqual(extras.io_state(1000, UNITS), 'on')
        # delegated by the system manager, but the running user manager does not hand it on: not "on"
        Machine(self, controllers='cpu io memory pids', subtree='cpu memory pids')
        self.assertEqual(extras.io_state(1000, UNITS), 'pending')
        Machine(self, unit_delegates='cpu io memory pids')
        self.assertEqual(extras.io_state(1000, UNITS), 'pending')
        Machine(self)
        self.assertEqual(extras.io_state(1000, UNITS), 'off')

    def test_an_unusual_manager_cgroup_is_unknown_not_on(self):
        # the cgroup root has io, but that says nothing about this manager
        m = Machine(self, manager='/user.slice/user-1000.slice/user@1000.service/app.slice/libpod-1.scope/x',
                    scheduler='[bfq] none')   # a disk that honours weights: only the manager is in doubt
        Machine.write(os.path.join(m.cg, 'cgroup.subtree_control'), 'cpu io memory')
        self.assertEqual(extras.manager_units(), [])
        self.assertEqual(extras.io_state(1000, []), 'unknown')
        lines = extras.report(cfg(), uid=1000)
        self.assertTrue(lines[0].startswith('root extras: disk weights unknown ('), lines[0])
        self.assertTrue(lines[1].startswith('root extras: desktop memory protection unknown ('), lines[1])
        with self.assertRaisesRegex(RuntimeError, 'not the usual'):
            extras.script(cfg(), uid=1000)
        Machine(self, manager='/user.slice/user-1000.slice/user@1000.service\n')
        self.assertEqual(extras.manager_units(), [])

    def test_iocost_and_protection(self):
        Machine(self, qos='8:0 enable=1 ctrl=auto\n259:0 enable=0 ctrl=auto', lows=(4 * GIB, 'max', 2 * GIB),
                desktop_low=3 * GIB)
        self.assertFalse(extras.iocost_on('259:0'))
        self.assertTrue(extras.iocost_on('8:0'))
        self.assertEqual(extras.protection(UNITS), 2 * GIB)
        self.assertEqual(extras.manager_units(), UNITS)
        Machine(self, lows=(4 * GIB,) * 3, desktop_low=0)   # parents protect, the desktop slices claim nothing
        self.assertEqual(extras.protection(UNITS), 0)

    def test_report_says_what_is_missing_and_where_the_script_is(self):
        Machine(self)
        lines = extras.report(cfg(), uid=1000)
        self.assertEqual(lines[0], 'root extras: disk weights off (io not delegated to the user manager; '
                                   'nvme0n1: no I/O scheduler and iocost off, so weights do not count)')
        self.assertIn('desktop memory protection off (user.slice, user-1000.slice, user@1000.service, app.slice, '
                      'session.slice protect at least 0 MiB; the desktop reserve is 4.0 GiB)', lines[1])
        self.assertIn('`agenthub root-extras > agenthub-root.sh`', lines[2])
        Machine(self, unit_delegates='cpu io memory pids', qos='259:0 enable=1')
        self.assertIn('disk weights pending (io delegated, in use once the user manager starts again (log out of '
                      'every session, or reboot); nvme0n1: iocost on)', extras.report(cfg(), uid=1000)[0])
        Machine(self, controllers='cpu io memory pids', subtree='cpu io memory pids',
                scheduler='mq-deadline [bfq] none', lows=(5 * GIB,) * 3, desktop_low=4 * GIB)
        self.assertEqual(extras.report(cfg(), uid=1000), [
            'root extras: disk weights on (io in use by the user manager; nvme0n1: BFQ scheduler)',
            'root extras: desktop memory protection on (user.slice, user-1000.slice, user@1000.service, app.slice, '
            'session.slice protect at least 4.0 GiB; the desktop reserve is 4.0 GiB)'])   # no script line
        Machine(self, controllers='cpu io memory pids', subtree='cpu io memory pids', layered=True)
        self.assertIn('disk weights unknown', extras.report(cfg('0'), uid=1000)[0])
        self.assertIn('protection off (desktop_reserve is 0)', extras.report(cfg('0'), uid=1000)[1])
        Machine(self, scheduler='[mq-deadline] none')
        self.assertIn('nvme0n1: the mq-deadline scheduler and iocost off', extras.report(cfg(), uid=1000)[0])


class ScriptTest(unittest.TestCase):
    def run_script(self, text, root=True):
        bin_dir = os.path.join(self.machine.dir, 'bin')
        os.makedirs(bin_dir, exist_ok=True)
        log = os.path.join(self.machine.dir, 'systemctl.log')
        stubs = {'systemctl': f'#!/bin/sh\necho "$@" >> {log}\n'}
        if root:
            stubs['id'] = '#!/bin/sh\necho 0\n'
        for name, body in stubs.items():
            Machine.write(os.path.join(bin_dir, name), body)
            os.chmod(os.path.join(bin_dir, name), 0o755)
        if not root and os.path.exists(os.path.join(bin_dir, 'id')):
            os.unlink(os.path.join(bin_dir, 'id'))
        path = os.path.join(self.machine.dir, 'extras.sh')
        Machine.write(path, text)
        self.assertEqual(subprocess.run(['sh', '-n', path]).returncode, 0)
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ['PATH'])
        return subprocess.run(['sh', path], env=env, capture_output=True, text=True, timeout=10)

    def files(self, etc):
        return sorted(os.path.relpath(os.path.join(d, f), etc) for d, _, fs in os.walk(etc) for f in fs)

    def test_adds_and_removes_the_extras(self):
        self.machine = Machine(self)
        etc = os.path.join(self.machine.dir, 'etc')
        text = extras.script(cfg(), uid=1000, etc=etc)
        self.assertNotIn('sudo', text)
        out = self.run_script(text)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.files(etc), [
            'systemd/system/user-1000.slice.d/50-agenthub-memory.conf',
            'systemd/system/user.slice.d/50-agenthub-memory.conf',
            'systemd/system/user@.service.d/50-agenthub-io.conf',
            'systemd/system/user@1000.service.d/50-agenthub-memory.conf',
            'tmpfiles.d/agenthub-iocost.conf'])

        def read(*path):
            with open(os.path.join(*path)) as f:
                return f.read()
        self.assertEqual(read(etc, 'systemd/system/user@.service.d/50-agenthub-io.conf'), '[Service]\nDelegate=io\n')
        self.assertEqual(read(etc, 'systemd/system/user@1000.service.d/50-agenthub-memory.conf'),
                         f'[Service]\nMemoryLow={4 * GIB}\n')
        self.assertEqual(read(etc, 'systemd/system/user.slice.d/50-agenthub-memory.conf'),
                         f'[Slice]\nMemoryLow={4 * GIB}\n')
        qos = os.path.join(self.machine.cg, 'io.cost.qos')
        self.assertEqual(read(etc, 'tmpfiles.d/agenthub-iocost.conf'), f'w {qos} - - - - 259:0 enable=1\n')
        self.assertEqual(read(qos), '259:0 enable=1\n')   # the whole disk, not the partition (259:5)
        out = self.run_script(extras.script(cfg(), undo=True, uid=1000, etc=etc))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.files(etc), [])
        self.assertEqual(os.listdir(os.path.join(etc, 'systemd', 'system')), [])   # its empty directories go too
        self.assertEqual(read(qos), '259:0 enable=0\n')
        self.assertEqual(read(self.machine.dir, 'systemctl.log'), 'daemon-reload\ndaemon-reload\n')
        out = self.run_script(extras.script(cfg(), undo=True, uid=1000, etc=etc))   # again: nothing left, no error
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_refused_iocost_write_stops_nothing_else(self):
        self.machine = Machine(self)
        etc = os.path.join(self.machine.dir, 'etc')
        text = extras.script(cfg(), uid=1000, etc=etc)
        qos = os.path.join(self.machine.cg, 'io.cost.qos')
        os.unlink(qos)
        os.mkdir(qos)   # a write to it fails, as the kernel's EINVAL would
        out = self.run_script(text)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('refused iocost for nvme0n1', out.stderr)
        self.assertNotIn('tmpfiles.d/agenthub-iocost.conf', self.files(etc))   # nothing to replay at boot
        self.assertIn('systemd/system/user.slice.d/50-agenthub-memory.conf', self.files(etc))
        Machine.write(os.path.join(etc, extras.TMPFILES), f'w {qos} - - - - 259:0 enable=1\n')
        out = self.run_script(extras.script(cfg(), undo=True, uid=1000, etc=etc))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('iocost stays on for 259:0 until reboot', out.stderr)
        self.assertEqual(self.files(etc), [])   # the removal still went all the way

    def test_disk_unknown_or_no_iocost_in_the_kernel(self):
        self.machine = Machine(self, layered=True)
        text = extras.script(cfg(), uid=1000)
        self.assertIn('could not be told', text)
        self.assertNotIn('io.cost.qos', '\n'.join(l for l in text.splitlines() if not l.startswith('#')))
        self.assertIn('disk weights only once the disk honours them', text)
        self.machine = Machine(self, qos=None)
        text = extras.script(cfg(), uid=1000)
        self.assertIn('this kernel has no iocost', text)
        self.assertNotIn('tmpfiles', '\n'.join(l for l in text.splitlines() if not l.startswith('#')))

    def test_leaves_alone_what_is_not_needed_or_not_its_own(self):
        self.machine = Machine(self, qos='259:0 enable=1 ctrl=user')   # the owner's own iocost setup
        etc = os.path.join(self.machine.dir, 'etc')
        out = self.run_script(extras.script(cfg('0'), uid=1000, etc=etc))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.files(etc), ['systemd/system/user@.service.d/50-agenthub-io.conf'])
        self.run_script(extras.script(cfg('0'), undo=True, uid=1000, etc=etc))
        with open(os.path.join(self.machine.cg, 'io.cost.qos')) as f:
            self.assertEqual(f.read(), '259:0 enable=1 ctrl=user')   # not ours: untouched
        self.machine = Machine(self, scheduler='mq-deadline [bfq] none')
        self.assertNotIn('io.cost.qos', '\n'.join(l for l in extras.script(cfg(), uid=1000).splitlines()
                                                    if not l.startswith('#')))

    def test_refuses_without_root(self):
        self.machine = Machine(self)
        etc = os.path.join(self.machine.dir, 'etc')
        out = self.run_script(extras.script(cfg(), uid=1000, etc=etc), root=False)
        self.assertEqual(out.returncode, 1)
        self.assertIn('run this as root', out.stderr)
        self.assertFalse(os.path.exists(etc))


class DesktopDropinsTest(unittest.TestCase):
    def test_written_removed_and_absent_without_a_reserve(self):
        self.assertEqual(install.desktop_dropins('agenthubtest', cfg()), {
            'app.slice.d/50-agenthubtest.conf': f'[Slice]\nMemoryLow={4 * GIB}\n',
            'session.slice.d/50-agenthubtest.conf': f'[Slice]\nMemoryLow={4 * GIB}\n'})
        self.assertEqual(install.desktop_dropins('agenthubtest', cfg('0')), {})
        with tempfile.TemporaryDirectory() as d:
            for name, text in install.desktop_dropins('agenthubtest', cfg()).items():
                Machine.write(os.path.join(d, name), text)
            Machine.write(os.path.join(d, 'session.slice.d', 'owner.conf'), '[Slice]\n')
            install.remove_dropins(d, 'agenthubtest')
            self.assertEqual(sorted(os.listdir(d)), ['session.slice.d'])   # the owner's drop-in stays
            self.assertEqual(os.listdir(os.path.join(d, 'session.slice.d')), ['owner.conf'])
            install.remove_dropins(d, 'agenthubtest')   # again: nothing there, no error

    def test_install_and_uninstall_keep_the_dropins_in_step_with_the_reserve(self):
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, 'config.toml')
            env = {'XDG_RUNTIME_DIR': d, 'XDG_STATE_HOME': d, 'AGENTHUB_PREFIX': 'agenthubtest',
                   'AGENTHUB_CONFIG': config}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(system, 'systemctl') as systemctl, \
                    mock.patch.object(extras, 'report', return_value=['root extras: (report)']):
                dropin = os.path.join(d, 'systemd', 'user', 'app.slice.d', '50-agenthubtest.conf')
                for reserve, present in (('3G', True), ('0', False), ('2G', True)):
                    Machine.write(config, f'[machine]\ndesktop_reserve = "{reserve}"\n')
                    with mock.patch('sys.stdout'):
                        self.assertEqual(install.install(runtime=True), 0)
                    self.assertEqual(os.path.exists(dropin), present, reserve)
                    self.assertEqual(os.path.isdir(os.path.dirname(dropin)), present, reserve)
                with open(dropin) as f:
                    self.assertEqual(f.read(), f'[Slice]\nMemoryLow={2 * GIB}\n')
                with mock.patch('sys.stdout'):
                    self.assertEqual(install.uninstall(runtime=True), 0)
                self.assertEqual(sorted(os.listdir(os.path.join(d, 'systemd', 'user'))), [])
                self.assertIn(mock.call('daemon-reload', check=True), systemctl.call_args_list)


if __name__ == '__main__':
    unittest.main()
