import os
import tempfile
import time
import unittest

from agenthub import history, pressure
from agenthub.scheduler import Run


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class GovernorTest(unittest.TestCase):
    def test_some_pauses_full_sheds_once_per_cooldown_calm_resumes(self):
        clock = Clock()
        g = pressure.Governor(clock)
        self.assertIsNone(g.assess())
        self.assertIsNone(g.paused)
        self.assertIsNone(g.assess(some='some 200 ms'))
        self.assertEqual(g.paused, pressure.PAUSED)
        self.assertEqual(g.assess(full='full 80 ms'), 'full 80 ms')
        clock.t += 5
        self.assertIsNone(g.assess(full='full 90 ms'))  # cooldown
        clock.t += pressure.COOLDOWN
        self.assertEqual(g.assess(full='full 90 ms'), 'full 90 ms')
        clock.t += pressure.CALM_FOR - 1
        self.assertIsNone(g.assess())
        self.assertEqual(g.paused, pressure.PAUSED)
        clock.t += 1
        self.assertIsNone(g.assess(headroom=0))  # calm but no room yet
        self.assertEqual(g.paused, pressure.PAUSED)
        self.assertIsNone(g.assess(headroom=1))
        self.assertIsNone(g.paused)


class SamplerTest(unittest.TestCase):
    def test_rates_are_per_two_seconds(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'memory.pressure')

            def write(some, full):
                with open(path, 'w') as f:
                    f.write(f'some avg10=0.00 avg60=0.00 avg300=0.00 total={some}\n'
                            f'full avg10=0.00 avg60=0.00 avg300=0.00 total={full}\n')
            s = pressure.Sampler(clock)
            write(1000, 500)
            self.assertIsNone(s.rates(path))
            clock.t += 4
            write(301000, 100500)
            self.assertEqual(s.rates(path), {'some': 150000, 'full': 50000})
            os.unlink(path)
            self.assertIsNone(s.rates(path))
            # the slice's cgroup came back with fresh counters: a new baseline first, no rate from the old one
            clock.t += 2
            write(10, 10)
            self.assertIsNone(s.rates(path))
            clock.t += 2
            write(20, 10)
            self.assertEqual(s.rates(path), {'some': 10, 'full': 0})
            # counters that went down (re-created between two samples) give no stall
            clock.t += 2
            write(5, 5)
            self.assertLess(max(s.rates(path).values()), 0)
            # the same instant twice: no rate rather than a division by zero
            write(6, 6)
            self.assertIsNone(s.rates(path))

    def test_system_file_parses(self):
        self.assertEqual(set(pressure.read_totals('/proc/pressure/memory')), {'some', 'full'})


class TriggersTest(unittest.TestCase):
    def test_arm_the_system_file_or_refuse_cleanly(self):
        t = pressure.Triggers()
        self.addCleanup(t.close)
        ok = t.arm('/proc/pressure/memory')
        self.assertEqual(t.armed('/proc/pressure/memory'), ok)
        self.assertEqual(len(t.fds), 2 if ok else 0)
        self.assertIsInstance(t.fired(), set)
        self.assertFalse(t.arm('/nonexistent/memory.pressure'))
        self.assertEqual(len(t.fds), 2 if ok else 0)

    def test_a_plain_file_refuses_triggers_and_leaks_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'memory.pressure')
            open(path, 'w').close()
            t = pressure.Triggers()
            self.addCleanup(t.close)
            before = len(os.listdir('/proc/self/fd'))
            self.assertFalse(t.arm(path))   # epoll does not take regular files
            self.assertEqual((t.fds, len(os.listdir('/proc/self/fd'))), ({}, before))


class FingerprintTest(unittest.TestCase):
    def test_program_subcommand_and_root(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'repo', '.git'))
            os.makedirs(os.path.join(d, 'repo', 'pkg', 'api'))
            sub = os.path.join(d, 'repo', 'pkg', 'api')
            root = os.path.realpath(os.path.join(d, 'repo'))
            self.assertEqual(history.fingerprint('npm run build -- --watch', sub), f'npm run build @ {root}')
            self.assertEqual(history.fingerprint('NODE_OPTIONS=--max-old-space-size=4096 npx tsc -p .', sub),
                             f'tsc @ {root}')
            self.assertEqual(history.fingerprint("bash -c 'cargo build --release'", sub), f'cargo build @ {root}')
            self.assertEqual(history.fingerprint('/usr/bin/pytest -x tests/a.py', sub), f'pytest @ {root}')
            self.assertEqual(history.fingerprint('echo "unclosed', d), f'echo @ {os.path.realpath(d)}')
            self.assertEqual(history.fingerprint('', d), f'? @ {os.path.realpath(d)}')
            self.assertEqual(history.fingerprint("python3 -c 'a = 1; b = 2' run", d),
                             f'python3 run @ {os.path.realpath(d)}')
            for wrapped in ('timeout 6 taskset -c 0-3 npm run build', 'nice -n 19 ionice -c 3 npm run build',
                            'timeout -s KILL 10m npm run build', 'taskset 0x3 npm run build',
                            'cd pkg && npm run build', 'npm ci; npm run build', '(cd pkg && npm run build)',
                            'npm run build 2>&1 | tee build.log', 'npm run build |& tee build.log',
                            'npm run build > build.log', 'npm run build &> build.log < /dev/null',
                            'env -i HOME=/x npm run build', 'env -u CI -C pkg npm run build'):
                self.assertEqual(history.fingerprint(wrapped, sub), f'npm run build @ {root}', wrapped)

    def test_p90(self):
        self.assertEqual(history.p90([5]), 5)
        self.assertEqual(history.p90(list(range(1, 11))), 9)
        self.assertEqual(history.p90(list(range(1, 21))), 18)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='agenthub-history-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.dir, ignore_errors=True))

    def run_with(self, peak, rid=1):
        now = time.time()
        return Run(id=rid, session='s', agent_id=None, cmd='x', cwd='/', kind='other', cls='p2', estimate=1,
                   requested=now - 10, started=now - 9, ended=now, peak=peak)

    def test_learns_p90_and_shed_counts_higher(self):
        h = history.History(os.path.join(self.dir, 'state', 'history.sqlite3'))
        self.addCleanup(h.close)
        self.assertEqual(oct(os.stat(h.path).st_mode & 0o777), '0o600')
        self.assertIsNone(h.estimate('fp'))
        for i, peak in enumerate((100, 300, 200)):
            h.record('fp', self.run_with(peak, i), shed=False)
        self.assertEqual(h.estimate('fp'), 300)
        h.record('fp', self.run_with(400, 9), shed=True)
        self.assertEqual(h.estimate('fp'), 600)
        h.record('other', self.run_with(None), shed=False)  # unknown peak: not recorded
        self.assertIsNone(h.estimate('other'))

    def test_an_existing_wider_file_is_narrowed(self):
        path = os.path.join(self.dir, 'h.sqlite3')
        open(path, 'w').close()
        os.chmod(path, 0o644)
        h = history.History(path)
        self.addCleanup(h.close)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o600')

    def test_keeps_a_bounded_tail(self):
        h = history.History(os.path.join(self.dir, 'h.sqlite3'))
        self.addCleanup(h.close)
        for i in range(history.KEEP_PER_FINGERPRINT + 15):
            h.record('fp', self.run_with(1000 + i, i), shed=False)
        self.assertEqual(h.db.execute('SELECT count(*) FROM runs').fetchone()[0], history.KEEP_PER_FINGERPRINT)

    def test_unusable_file_disables_learning_only(self):
        blocker = os.path.join(self.dir, 'file')
        open(blocker, 'w').close()
        h = history.History(os.path.join(blocker, 'h.sqlite3'))  # parent is a file
        self.assertIsNone(h.db)
        h.record('fp', self.run_with(5), shed=False)
        self.assertIsNone(h.estimate('fp'))


if __name__ == '__main__':
    unittest.main()
