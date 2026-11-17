"""Against the real systemd user manager. Opt-in: AGENTHUB_IT=1.

Everything uses the prefix `agenthubtest` and runtime units (gone after reboot) and is removed at the end.
Load stays small: `sleep`, CPU burners pinned to one core under `timeout 6`, and memory hogs of at most
~650 MiB in total, confined to the test slice with swap turned off for it (the storm test runs last).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PFX = 'agenthubtest'
RUNTIME = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
CONFIG = """
[machine]
desktop_reserve = 0
agent_budget = "600M"
default_estimate = "10M"
idle_exit = "3s"
system_pressure = false   # the desktop's own pressure must not steer these tests

[pools]
browser = 1
"""
# allocates SIZE MiB (touching every page), STEP MiB every 50 ms when a step is given, then holds it for HOLD s
HOG = """
import sys, time
size, hold, step = int(sys.argv[1]) << 20, float(sys.argv[2]), int(sys.argv[3]) << 20
chunks, got = [], 0
while got < size:
    n = min(step or size, size - got)
    chunks.append(b'\\x01' * n)
    got += n
    if step:
        time.sleep(0.05)
time.sleep(hold)
"""


def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=20)


@unittest.skipUnless(os.environ.get('AGENTHUB_IT') == '1', 'integration tests need AGENTHUB_IT=1')
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='agenthub-it-')
        cls.config = os.path.join(cls.tmp, 'config.toml')
        with open(cls.config, 'w') as f:
            f.write(CONFIG)
        cls.env = dict(os.environ, AGENTHUB_PREFIX=PFX, AGENTHUB_CONFIG=cls.config, PYTHONPATH=ROOT)
        sys.path.insert(0, ROOT)
        from agenthub import history, system
        cls.history = history.default_path(PFX)
        cls.cg = system.Cgroups(PFX)
        cls.env.pop('AGENTHUB_SOCKET', None)
        cls.env.pop('AGENTHUB_SESSION', None)
        out = cls.cli('install', '--runtime')
        assert out.returncode == 0, out.stderr
        cls.start_sessions()

    @classmethod
    def start_sessions(cls):
        for sid, klass in (('hi', 'p1'), ('mid', 'p2'), ('lo', 'p3')):
            cls.cli('session', 'start', sid, '--cwd', cls.tmp)
            cls.cli('session', 'prio', sid, klass)

    @classmethod
    def tearDownClass(cls):
        for sid in ('hi', 'mid', 'lo'):
            cls.cli('session', 'end', sid)
        systemctl('stop', f'{PFX}-run-*.scope', f'{PFX}-direct-*.scope')
        cls.cli('uninstall', '--runtime')
        for unit in (f'{PFX}.slice', f'{PFX}-p0.slice', f'{PFX}-p1.slice', f'{PFX}-p2.slice', f'{PFX}-p3.slice'):
            systemctl('stop', unit)
        shutil.rmtree(os.path.join(RUNTIME, PFX), ignore_errors=True)
        shutil.rmtree(os.path.dirname(cls.history), ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def cli(cls, *args, env=None):
        return subprocess.run([sys.executable, '-m', 'agenthub', *args], capture_output=True, text=True,
                              env=env or cls.env, cwd=cls.tmp, timeout=60)

    def bg(self, *args):
        proc = subprocess.Popen([sys.executable, '-m', 'agenthub', 'run', *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env, cwd=self.tmp)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def status(self):
        out = self.cli('status', '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def until(self, predicate, timeout=15, what='condition'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        st = self.status()
        brief = {k: [(r['id'], r.get('session'), r.get('state'), r.get('reason')) for r in st[k]]
                 for k in ('runs', 'queue', 'recent')}
        self.fail(f'timed out waiting for {what}; hub sees {brief}, sessions {[s["id"] for s in st["sessions"]]}')

    def running(self, session, cmd_part):
        return next((r for r in self.status()['runs']
                     if r['session'] == session and cmd_part in r['cmd'] and r['state'] == 'running'), None)

    def test_1_lower_class_waits_for_memory_and_no_overtaking(self):
        a = self.bg('--session', 'hi', '--estimate', '400M', '--', 'sh', '-c', 'cat /proc/self/cgroup; sleep 4')
        first = self.until(lambda: self.running('hi', 'sleep 4'), what='run of hi')
        m = self.bg('--session', 'mid', '--estimate', '400M', '--', 'sleep', '1')
        # lo must ask after mid is queued: asked first, it would fit and rightly start at once
        self.until(lambda: self.status()['queue'], what='mid queued')
        lo = self.bg('--session', 'lo', '--estimate', '100M', '--', 'sleep', '1')
        queue = self.until(lambda: len(self.status()['queue']) == 2 and self.status()['queue'], what='two queued')
        self.assertEqual([q['session'] for q in queue], ['mid', 'lo'])
        self.assertIn('needs 400 MiB', queue[0]['reason'])
        self.assertIn('behind run', queue[1]['reason'])
        out, err = a.communicate(timeout=20)
        self.assertEqual(a.returncode, 0, err)
        self.assertIn(f'/{PFX}.slice/{PFX}-p1.slice/{PFX}-run-{first["id"]}.scope', out)
        for proc in (m, lo):
            _, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn('queued #', err)

    def test_2_cpu_is_shared_by_class_weight(self):
        burn = ['timeout', '6', 'taskset', '-c', '3', sys.executable, '-c', 'while True: pass']
        hi = self.bg('--session', 'hi', '--', *burn)
        lo = self.bg('--session', 'lo', '--', *burn)
        self.until(lambda: self.running('hi', 'while True') and self.running('lo', 'while True'), what='both burners')
        time.sleep(3)
        runs = {r['session']: r['cpu_usec'] for r in self.status()['runs'] if 'while True' in r['cmd']}
        share = runs['hi'] / (runs['hi'] + runs['lo'])
        for proc in (hi, lo):
            proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 124)
        self.assertGreaterEqual(share, 0.8, f'p1 got {share:.0%} of one core against p3')

    def test_3_hub_restart_loses_nothing(self):
        long = self.bg('--session', 'hi', '--estimate', '50M', '--', 'sleep', '8')
        run = self.until(lambda: self.running('hi', 'sleep 8'), what='long run')
        waiting = self.bg('--session', 'lo', '--estimate', '590M', '--', 'sleep', '1')
        self.until(lambda: self.status()['queue'], what='queued run')
        restarts = int(systemctl('show', f'{PFX}.service', '-p', 'NRestarts', '--value').stdout.strip() or 0)
        systemctl('kill', '--kill-whom=main', '-s', 'KILL', f'{PFX}.service')
        self.until(lambda: int(systemctl('show', f'{PFX}.service', '-p', 'NRestarts', '--value').stdout.strip() or 0)
                   > restarts, what='systemd to restart the hub')
        after = self.until(lambda: self.running('hi', 'sleep 8'), what='run after restart')
        self.assertEqual((after['id'], after['estimate'], after['class']), (run['id'], run['estimate'], 'p1'))
        self.assertEqual(self.until(lambda: self.status()['queue'], what='queue after restart')[0]['session'], 'lo')
        _, err = long.communicate(timeout=20)
        self.assertEqual(long.returncode, 0, err)
        _, err = waiting.communicate(timeout=20)
        self.assertEqual(waiting.returncode, 0, err)
        self.assertIn('queueing again', err)

    def test_4_fails_open(self):
        systemctl('stop', f'{PFX}.socket', f'{PFX}.service')
        self.addCleanup(systemctl, 'start', f'{PFX}.socket')
        out = self.cli('run', '--', 'sh', '-c', 'cat /proc/self/cgroup; exit 7')
        self.assertEqual(out.returncode, 7)
        self.assertIn('running without the hub', out.stderr)
        self.assertIn(f'/{PFX}-p2.slice/{PFX}-direct-', out.stdout)
        bad = os.path.join(self.tmp, 'bad.toml')
        with open(bad, 'w') as f:
            f.write('[machine]\nagent_budget = \n')
        env = dict(self.env, AGENTHUB_CONFIG=bad, AGENTHUB_SOCKET=os.path.join(self.tmp, 'bad.sock'))
        hub = self.cli('hub', env=env)
        self.assertEqual(hub.returncode, 78)
        self.assertIn(bad, hub.stderr)
        self.assertIn('line 2', hub.stderr)
        out = self.cli('run', '--', 'true', env=env)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_4b_broken_config_under_socket_activation(self):
        with open(self.config) as f:
            good = f.read()
        service = f'{PFX}.service'
        restarts = systemctl('show', service, '-p', 'NRestarts', '--value').stdout.strip()

        def restore():
            with open(self.config, 'w') as f:
                f.write(good)
            systemctl('reset-failed', service, f'{PFX}.socket')
            systemctl('start', f'{PFX}.socket')
        self.addCleanup(restore)
        systemctl('stop', service)
        with open(self.config, 'w') as f:
            f.write('[machine]\nagent_budget = \n')
        started = time.monotonic()
        out = self.cli('run', '--', 'sh', '-c', 'exit 4')
        self.assertEqual(out.returncode, 4, out.stderr)
        self.assertIn('running without the hub', out.stderr)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(systemctl('show', service, '-p', 'Result', '--value').stdout.strip(), 'exit-code')
        self.assertEqual(systemctl('show', service, '-p', 'NRestarts', '--value').stdout.strip(), restarts)

    def test_5_refused_expired_and_stopped(self):
        self.assertEqual(self.cli('run', '--session', 'hi', '--estimate', '5G', '--', 'true').returncode, 69)
        holder = self.bg('--session', 'hi', '--estimate', '590M', '--', 'sleep', '30')
        run = self.until(lambda: self.running('hi', 'sleep 30'), what='holder')
        late = self.cli('run', '--session', 'lo', '--estimate', '100M', '--wait', '1s', '--', 'true')
        self.assertEqual(late.returncode, 75, late.stderr)
        self.assertIn('wait_expired', late.stderr)
        self.assertEqual(self.cli('stop', str(run['id']), '--reason', 'test').returncode, 0)
        _, err = holder.communicate(timeout=20)
        self.assertEqual(holder.returncode, 75, err)
        self.assertIn('stopped by the hub: test', err)

    def test_6_idle_exit_and_socket_activation(self):
        for sid in ('hi', 'mid', 'lo'):
            self.cli('session', 'end', sid)
        self.addCleanup(self.start_sessions)   # with their classes: the tests after this one rely on them
        self.until(lambda: systemctl('is-active', f'{PFX}.service').stdout.strip() != 'active', timeout=20,
                   what='hub to exit when idle')
        self.assertEqual(self.status()['sessions'], [])
        self.assertEqual(systemctl('is-active', f'{PFX}.service').stdout.strip(), 'active')

    def test_7_admissions_pause_by_hand(self):
        self.addCleanup(self.cli, 'admissions', 'resume')
        out = self.cli('admissions', 'pause', '--reason', 'test')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('admissions paused', out.stdout)
        held = self.bg('--session', 'hi', '--estimate', '10M', '--', 'true')
        queue = self.until(lambda: self.status()['queue'], what='paused queue')
        self.assertEqual(queue[0]['reason'], 'admissions paused by hand (test)')
        self.assertEqual(self.cli('admissions', 'resume').returncode, 0)
        _, err = held.communicate(timeout=20)
        self.assertEqual(held.returncode, 0, err)

    def test_8_pools_and_learned_estimates(self):
        hog = ['--', sys.executable, '-c', HOG, '150', '4', '0']
        first = self.bg('--session', 'hi', '--estimate', '10M', '--pool', 'browser', *hog)
        run = self.until(lambda: self.running('hi', 'time.sleep'), what='first pool holder')
        second = self.bg('--session', 'mid', '--estimate', '10M', '--pool', 'browser', '--', 'true')
        queue = self.until(lambda: self.status()['queue'], what='second waits for the pool')
        self.assertEqual(queue[0]['reason'], f'pool browser 1/1 held by run {run["id"]}')
        plain = self.cli('run', '--session', 'lo', '--estimate', '10M', '--', 'true')
        self.assertEqual(plain.returncode, 0, plain.stderr)   # not held up by the pool queue
        for proc in (first, second):
            _, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
        again = self.cli('run', '--session', 'hi', '--estimate', '10M', *hog)
        self.assertEqual(again.returncode, 0, again.stderr)
        learned = self.bg('--session', 'hi', *hog)   # no --estimate: learned from the two runs above
        run = self.until(lambda: self.running('hi', 'time.sleep'), what='learned run')
        why = json.loads(self.cli('why', str(run['id'])).stdout)
        self.assertEqual(why['estimate_source'], 'history')
        self.assertGreaterEqual(why['estimate'], 150 << 20)
        learned.communicate(timeout=30)
        self.assertEqual(oct(os.stat(self.history).st_mode & 0o777), '0o600')

    def test_9_memory_storm_sheds_exactly_one_run(self):
        """Three hogs in the 600M budget; the newest p3 one grows past its MemoryHigh with swap off for the slice."""
        with open('/proc/meminfo') as f:
            available = next(int(line.split()[1]) << 10 for line in f if line.startswith('MemAvailable:'))
        if available < 4 << 30:
            self.skipTest(f'only {available >> 20} MiB available; the storm test wants 4 GiB of room')
        with open('/proc/pressure/memory') as f:
            full = float(f.read().split('full avg10=')[1].split()[0])
        if full > 5:
            self.skipTest(f'the machine is already under memory pressure (full avg10 {full}%)')
        slice_ = f'{PFX}.slice'
        self.assertEqual(systemctl('set-property', '--runtime', slice_, 'MemorySwapMax=0').returncode, 0)
        self.addCleanup(systemctl, 'revert', slice_)

        worst, stop = [0.0], threading.Event()

        def probe():   # stands in for the desktop: wakes every 50 ms and touches fresh memory
            while not stop.is_set():
                started = time.monotonic()
                bytearray(b'\x01') * (1 << 20)
                time.sleep(0.05)
                worst[0] = max(worst[0], time.monotonic() - started - 0.05)
        watcher = threading.Thread(target=probe, daemon=True)
        watcher.start()
        self.addCleanup(stop.set)
        with open('/proc/pressure/memory') as f:
            full_before = int(f.read().split('full ')[1].split('total=')[1].split()[0])
        began = time.monotonic()

        p1 = self.bg('--session', 'hi', '--estimate', '50M', '--', sys.executable, '-c', HOG, '150', '15', '0')
        hi = self.until(lambda: self.running('hi', 'time.sleep'), what='p1 hog')
        self.assertEqual(hi['class'], 'p1')
        with open(os.path.join(self.cg.base, 'memory.swap.max')) as f:
            swap = f.read().strip()
        self.assertEqual(swap, '0', 'swap must be off for the test slice, or the hogs would spill into it')
        timeout = systemctl('show', hi['unit'], '-p', 'TimeoutStopUSec', '--value').stdout.strip()
        self.assertEqual(timeout, '10s')
        old = self.bg('--session', 'lo', '--estimate', '50M', '--', sys.executable, '-c', HOG, '150', '15', '0')
        self.assertEqual(self.until(lambda: self.running('lo', 'time.sleep'), what='first p3 hog')['class'], 'p3')
        self.until(lambda: (self.status()['machine']['agent_current'] or 0) >= 300 << 20, what='300M held')
        new = self.bg('--session', 'lo', '--estimate', '50M', '--', sys.executable, '-c', HOG, '320', '15', '10')
        _, err = new.communicate(timeout=40)
        self.assertEqual(new.returncode, 75, err)
        self.assertRegex(err, r'stopped by the hub: shed: memory full-pressure .*\(agents')
        for proc in (p1, old):
            _, err = proc.communicate(timeout=40)
            self.assertEqual(proc.returncode, 0, err)
        stop.set()
        watcher.join(2)
        with open('/proc/pressure/memory') as f:
            full_after = int(f.read().split('full ')[1].split('total=')[1].split()[0])
        shed = [r for r in self.status()['recent'] if (r.get('stopping') or '').startswith('shed')]
        self.assertEqual(len(shed), 1, shed)
        self.assertEqual((shed[0]['session'], shed[0]['class']), ('lo', 'p3'))
        rows = sqlite3.connect(self.history).execute('SELECT shed FROM runs WHERE shed = 1').fetchall()
        self.assertEqual(len(rows), 1)
        system_full = (full_after - full_before) / 1e6 / (time.monotonic() - began)
        print(f'\nstorm: probe worst delay {worst[0] * 1000:.0f} ms, system full-pressure {system_full:.1%}',
              file=sys.stderr)
        self.assertLess(worst[0], 0.5, 'the desktop stand-in stalled')


if __name__ == '__main__':
    unittest.main()
