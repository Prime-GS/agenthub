"""Against the real systemd user manager. Opt-in: AGENTHUB_IT=1.

Everything uses the prefix `agenthubtest` and runtime units (gone after reboot) and is removed at the end.
Load stays small: `sleep`, and CPU burners pinned to one core under `timeout 6`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        cls.env.pop('AGENTHUB_SOCKET', None)
        cls.env.pop('AGENTHUB_SESSION', None)
        out = cls.cli('install', '--runtime')
        assert out.returncode == 0, out.stderr
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
        self.addCleanup(lambda: [self.cli('session', 'start', s, '--cwd', self.tmp) for s in ('hi', 'mid', 'lo')])
        self.until(lambda: systemctl('is-active', f'{PFX}.service').stdout.strip() != 'active', timeout=20,
                   what='hub to exit when idle')
        self.assertEqual(self.status()['sessions'], [])
        self.assertEqual(systemctl('is-active', f'{PFX}.service').stdout.strip(), 'active')


if __name__ == '__main__':
    unittest.main()
