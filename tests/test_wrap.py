"""`agenthub wrap` and its shims, as an agent without hooks meets them: real processes, a real hub over its socket
(fake cgroups and memory), and no systemd scopes ($XDG_RUNTIME_DIR points at a temp dir)."""
import os
import subprocess
import sys
import unittest

from agenthub import client, wrap
from tests.test_hub import M, World, until

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KINDS = [{'name': 'build', 'match': '^make build', 'estimate': '100M'}]
CONFIG = """[machine]
agent_budget = "1000M"
desktop_reserve = 0

[[kinds]]
name = "build"
match = '^make build'
estimate = "100M"
"""
# a stand-in for make: shows how it was started
FAKE_MAKE = """#!/bin/sh
echo "make $* run=${AGENTHUB_RUN:-none} session=${AGENTHUB_SESSION:-none}"
[ -n "$SHOW" ] && echo "pythonpath=$PYTHONPATH $(grep SigIgn /proc/self/status)"
exit ${FAKE_EXIT:-0}
"""


class WrapTest(unittest.TestCase):
    def setUp(self):
        self.w = World(kinds=KINDS)
        self.addCleanup(self.w.cleanup)
        self.hub, thread = self.w.start()
        self.addCleanup(self.w.stop, self.hub, thread)
        self.runtime = os.path.join(self.w.dir, 'runtime')
        self.bin = os.path.join(self.w.dir, 'bin')
        os.makedirs(self.runtime)
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, 'make'), 'w') as f:
            f.write(FAKE_MAKE)
        os.chmod(os.path.join(self.bin, 'make'), 0o755)
        config = os.path.join(self.w.dir, 'config.toml')
        with open(config, 'w') as f:
            f.write(CONFIG)
        self.env = dict(os.environ, AGENTHUB_SOCKET=self.w.sock, AGENTHUB_PREFIX='agenthubtest',
                        AGENTHUB_CONFIG=config, XDG_RUNTIME_DIR=self.runtime, PYTHONPATH=ROOT,
                        PATH=self.bin + os.pathsep + os.environ['PATH'])
        for name in ('AGENTHUB_SESSION', 'AGENTHUB_RUN', 'AGENTHUB_SESSION_PID', 'AGENTHUB_AGENT'):
            self.env.pop(name, None)
        self.shims = os.path.join(self.runtime, 'agenthubtest', 'shims')

    def agenthub(self, *args, **env):
        return subprocess.run([sys.executable, '-P', '-m', 'agenthub', *args], capture_output=True, text=True,
                              env=dict(self.env, **env), cwd=self.w.dir, timeout=30)

    def wrapped(self, script, *, agent='codex', **env):
        return self.agenthub('wrap', '--agent', agent, '--', 'sh', '-c', script, **env)

    def test_wrapped_agent_sends_heavy_commands_through_the_hub(self):
        out = self.wrapped('command -v make; make clean; make build; echo "session $AGENTHUB_SESSION"')
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0], os.path.join(self.shims, 'make'))   # the shim comes first in PATH
        sid = lines[-1].split()[1]
        self.assertRegex(sid, r'^codex-[0-9a-f]{12}$')
        self.assertEqual(lines[1], f'make clean run=none session={sid}')   # light: straight to the tool
        runs = list(self.hub.sched.runs.values())
        self.assertEqual([(r.cmd, r.kind, r.session, r.state, r.exit) for r in runs],
                         [('make build', 'build', sid, 'ended', 0)])
        self.assertEqual(lines[2], f'make build run={runs[0].id} session={sid}')
        # the session belonged to the wrapped process: it ended with it
        self.assertTrue(until(lambda: sid not in self.hub.sched.sessions))

    def test_session_carries_the_agent_and_its_pid(self):
        proc = subprocess.Popen([sys.executable, '-P', '-m', 'agenthub', 'wrap', '--agent', 'My Agent/2', '--',
                                 'sleep', '30'], env=self.env, cwd=self.w.dir, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertTrue(until(lambda: self.hub.sched.sessions))
        session = next(iter(self.hub.sched.sessions.values()))
        self.assertRegex(session.id, r'^My-Agent-2-[0-9a-f]{12}$')
        self.assertEqual((session.agent, session.pid, session.cwd), ('My-Agent-2', proc.pid, self.w.dir))

    def test_a_tool_inside_a_run_does_not_queue_again(self):
        wrap.write_shims(self.shims)
        out = self.agenthub('run', '--estimate', '50M', '--', 'sh', '-c', f'PATH={self.shims}:$PATH; make build',
                            AGENTHUB_SESSION='s1')
        self.assertEqual(out.returncode, 0, out.stderr)
        runs = list(self.hub.sched.runs.values())
        self.assertEqual(len(runs), 1)   # the outer run only
        self.assertIn(f'make build run={runs[0].id} session=s1', out.stdout)

    def test_hub_away_unknown_session_and_missing_tool(self):
        # the hub forgot the session (or was away at wrap time): `agenthub run` registers it with the agent's pid
        wrap.write_shims(self.shims)
        out = self.agenthub('shim', 'make', 'build', AGENTHUB_SESSION='codex-x', AGENTHUB_AGENT='codex',
                            AGENTHUB_SESSION_PID=str(os.getpid()))
        self.assertEqual(out.returncode, 0, out.stderr)
        session = self.hub.sched.sessions['codex-x']
        self.assertEqual((session.agent, session.pid), ('codex', os.getpid()))
        # no hub at all: the wrapped agent still runs, and so do its builds
        away = self.wrapped('make build; exit 3', AGENTHUB_SOCKET=os.path.join(self.w.dir, 'none.sock'))
        self.assertEqual(away.returncode, 3)
        self.assertIn('hub not reachable', away.stderr)
        self.assertIn('make build run=direct', away.stdout)
        # a tool that is not installed, and an agent that is not
        out = self.agenthub('shim', 'cargo', 'build', PATH=self.shims + os.pathsep + self.bin + ':/usr/bin:/bin')
        self.assertEqual((out.returncode, out.stderr.strip()), (127, 'cargo: command not found'))
        self.assertEqual(self.agenthub('wrap', '--', 'no-such-agent-here').returncode, 127)
        self.assertEqual(self.agenthub('wrap').returncode, 2)

    def test_the_tool_gets_the_environment_and_signals_of_the_agent(self):
        pythonpath = f'/user/lib{os.pathsep}{ROOT}'
        out = self.wrapped('make clean; make build', SHOW='1', PYTHONPATH=pythonpath)
        self.assertEqual(out.returncode, 0, out.stderr)
        shown = [line for line in out.stdout.splitlines() if line.startswith('pythonpath=')]
        self.assertEqual(len(shown), 2)
        for line in shown:   # light (exec) and heavy (through the hub) alike
            path, ignored = line.split()[0], int(line.split()[-1], 16)
            self.assertEqual(path, f'pythonpath={pythonpath}')
            self.assertEqual(ignored & ((1 << 12) | (1 << 24)), 0, line)   # SIGPIPE, SIGXFSZ not ignored

    def test_the_hub_decides_the_kind(self):
        # the client's config knows a kind the hub's does not: the command still runs, as kind `other`
        self.w.cfg.default_estimate = 10 * M
        with open(self.env['AGENTHUB_CONFIG'], 'a') as f:
            f.write('\n[[kinds]]\nname = "test"\nmatch = \'^make test\'\nestimate = "100M"\n')
        out = self.wrapped('make test')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([(r.cmd, r.kind) for r in self.hub.sched.runs.values()], [('make test', 'other')])

    def test_exit_codes_pass_through(self):
        self.assertEqual(self.wrapped('make build', FAKE_EXIT='5').returncode, 5)
        self.assertEqual(self.wrapped('make clean', FAKE_EXIT='6').returncode, 6)

    def test_names(self):
        self.assertEqual(client.agent_name('Visual Studio Code'), 'Visual-Studio-Code')
        self.assertEqual(client.agent_name('../é/'), 'agent')
        self.assertEqual(client.agent_name(''), 'agent')
        self.assertEqual(len(client.agent_name('x' * 300)), 40)
        self.assertNotIn('python3', wrap.TOOLS)   # an interpreter would start one more on every call


if __name__ == '__main__':
    unittest.main()
