"""The Claude Code hooks against a real hub over its socket (fake cgroups, fake memory), and their pure parts."""
import argparse
import contextlib
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from agenthub import claude, client, system
from tests.test_hub import M, World, until


def rules(allow=(), ask=(), deny=()):
    return {'allow': list(allow), 'ask': list(ask), 'deny': list(deny)}


class JudgeTest(unittest.TestCase):
    def test_allow_needs_every_part_to_match(self):
        allow = rules(allow=['npm run build:*', 'git status', 'make *'])
        self.assertEqual(claude.judge('npm run build -- --watch', allow), 'allow')
        self.assertEqual(claude.judge('npm run build', allow), 'allow')
        self.assertEqual(claude.judge('make -j8 all', allow), 'allow')
        self.assertEqual(claude.judge('git status && npm run build', allow), 'allow')
        self.assertIsNone(claude.judge('npm run buildx', allow))            # a prefix ends at a word
        self.assertIsNone(claude.judge('cd api && npm run build', allow))   # `cd api` is not allowed by any rule
        self.assertIsNone(claude.judge('npm run build; rm -rf x', allow))
        self.assertIsNone(claude.judge('NODE_ENV=x npm run build', allow))

    def test_complex_lines_are_never_allowed(self):
        everything = rules(allow=['*'])
        self.assertEqual(claude.judge('npm test', everything), 'allow')
        for line in ('npm test $(cat args)', 'npm test `cat args`', 'npm test\nrm x', 'diff <(make a) b',
                     'cat <<EOF | make\nx\nEOF', 'npm test "unclosed'):
            self.assertIsNone(claude.judge(line, everything), line)

    def test_a_second_command_cannot_hide_in_the_line(self):
        make = rules(allow=['make:*', 'tail:*'])
        for line in ('make&&(touch /tmp/x)', 'make;(id)', 'make &&>x id', 'make a#;id', 'make x#&&id',
                     'make \\;id', "make \\' ; id ; '", 'make "\\"" ; id ; ""', 'make ; id', 'make | sh',
                     'make & id', 'make |& id', 'make || id', 'make > out.log', 'make 2>out.log', 'make >| x',
                     'make &> x', '; make', 'make && && make', 'make\rid', "make $'\\'' ; id ; '$'\\''"):
            self.assertIsNone(claude.judge(line, make), line)
        for line in ('make "x;id"', "make 'a&&b' c", 'make 2>&1 | tail -5', 'make > /dev/null 2>&1', 'make &',
                     'make 2>/dev/null; make clean', 'make </dev/null &>/dev/null', 'make ${TARGET}'):
            self.assertEqual(claude.judge(line, make), 'allow', line)

    def test_deny_and_ask_are_matched_broadly(self):
        self.assertEqual(claude.judge('cd x && npm run build', rules(allow=['*'], deny=['npm run build:*'])), 'deny')
        self.assertEqual(claude.judge('make deploy', rules(ask=['make deploy'])), 'ask')
        self.assertEqual(claude.judge('anything', rules(deny=['*'])), 'deny')
        self.assertIsNone(claude.judge('npm test', rules(deny=['git push:*'], ask=['rm *'])))
        deny = rules(allow=['*'], deny=['rm -rf:*', 'git push:*'])
        for line in ('make && \\rm -rf ~', 'make && rm  -rf ~', 'make && git  push', "make && 'git' push",
                     'bash -c rm\\ -rf\\ ~', 'make;g""it push', 'make&&rm -rf ~'):
            self.assertEqual(claude.judge(line, deny), 'deny', line)
        # a line that cannot be split is about any rule there is
        self.assertEqual(claude.judge('make $(cat targets)', deny), 'deny')
        self.assertEqual(claude.judge('make $(cat targets)', rules(ask=['git push:*'])), 'ask')
        self.assertIsNone(claude.judge('make $(cat targets)', rules(allow=['make:*'])))

    def test_settings_files_are_read_and_a_broken_one_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, 'good.json')
            with open(good, 'w') as f:
                json.dump({'permissions': {'allow': ['Bash(npm test:*)', 'Read', 'Bash'], 'deny': ['Bash(rm:*)']}}, f)
            self.assertEqual(claude.bash_rules([good, os.path.join(d, 'missing.json')]),
                             rules(allow=['npm test:*', '*'], deny=['rm:*']))
            bad = os.path.join(d, 'bad.json')
            with open(bad, 'w') as f:
                f.write('{')
            with self.assertRaises(ValueError):
                claude.bash_rules([bad])
            # a user-level settings.local.json may belong to another project: deny and ask only
            self.assertEqual(claude.bash_rules([(good, ('ask', 'deny'))]), rules(deny=['rm:*']))

    def test_rules_given_on_claude_code_command_line(self):
        argv = [sys.executable, '-c', 'import time; time.sleep(30)', '-p', 'do it', '--disallowedTools',
                'Bash(git push:*)', 'Read', '--allowedTools=Bash(npm test:*),Edit', '--allowed-tools', 'Bash(make:*)',
                'Bash(ls)', '--model', 'x', '--settings', '{"permissions": {"ask": ["Bash(cargo:*)"]}}',
                '--setting-sources', 'project']
        proc = subprocess.Popen(argv)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        until(lambda: claude.cli_options(proc.pid)['sources'] is not None)   # python has its argv
        cli = claude.cli_options(proc.pid)
        self.assertEqual(cli, {'allow': ['Bash(npm test:*)', 'Edit', 'Bash(make:*)', 'Bash(ls)'],
                               'deny': ['Bash(git push:*)', 'Read'],
                               'settings': ['{"permissions": {"ask": ["Bash(cargo:*)"]}}'], 'sources': 'project'})
        self.assertEqual(claude.bash_rules([], cli), rules(allow=['npm test:*', 'make:*', 'ls'], ask=['cargo:*'],
                                                           deny=['git push:*']))
        self.assertEqual([os.path.basename(p) for p, _ in claude.settings_files('/w', cli['sources'])],
                         ['managed-settings.json', 'settings.local.json', 'settings.json'])
        self.assertEqual(claude.cli_options(2 ** 22 + 1), {'allow': [], 'deny': [], 'settings': [], 'sources': None})


class PidTest(unittest.TestCase):
    def test_claude_pid_trusts_the_variable_only_for_an_ancestor(self):
        chain = [pid for pid, _ in claude.ancestors(os.getppid())]
        with mock.patch.dict(os.environ, {'CLAUDE_PID': str(chain[-1])}):
            self.assertEqual(claude.claude_pid(), chain[-1])
        with mock.patch.dict(os.environ, {'CLAUDE_PID': str(os.getpid())}):   # not an ancestor of the hook
            self.assertIn(claude.claude_pid(), chain)


class HookTest(unittest.TestCase):
    """Each hook as Claude Code calls it: JSON in, JSON (or nothing) out, the hub on the other side."""

    def setUp(self):
        self.w = World(budget=7000 * M)
        self.addCleanup(self.w.cleanup)
        self.home = os.path.join(self.w.dir, 'claude-home')
        self.project = os.path.join(self.w.dir, 'project')
        os.makedirs(os.path.join(self.project, '.claude'))
        os.makedirs(self.home)
        self.env_file = os.path.join(self.w.dir, 'env.sh')
        env = mock.patch.dict(os.environ, {
            'AGENTHUB_SOCKET': self.w.sock, 'AGENTHUB_PREFIX': 'agenthubtest',
            'AGENTHUB_CONFIG': os.path.join(self.w.dir, 'none.toml'), 'CLAUDE_CONFIG_DIR': self.home,
            'CLAUDE_PROJECT_DIR': self.project, 'CLAUDE_ENV_FILE': self.env_file})
        env.start()
        self.addCleanup(env.stop)
        state = mock.patch('agenthub.system.state_dir', return_value=self.w.state)
        state.start()
        self.addCleanup(state.stop)

    def start(self):
        hub, thread = self.w.start()
        self.addCleanup(self.w.stop, hub, thread)
        return hub

    def hook(self, event, **data):
        data.setdefault('session_id', 'claude-1')
        data.setdefault('cwd', self.project)
        out = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            self.assertEqual(claude.main(event), 0)
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def bash(self, command, **extra):
        return self.hook('pre-tool-use', tool_name='Bash', permission_mode='default',
                         tool_input={'command': command, 'description': 'd', 'timeout': 600000, **extra})

    def settings(self, **perms):
        with open(os.path.join(self.project, '.claude', 'settings.json'), 'w') as f:
            json.dump({'permissions': perms}, f)

    def test_session_start_registers_and_exports_the_session(self):
        hub = self.start()
        self.assertIsNone(self.hook('session-start', source='startup'))
        session = hub.sched.sessions['claude-1']
        self.assertEqual((session.agent, session.cwd), ('claude-code', self.project))
        self.assertIn(session.pid, [pid for pid, _ in claude.ancestors(os.getppid())])
        with open(self.env_file) as f:
            self.assertEqual(f.read(), 'export AGENTHUB_SESSION=claude-1\n')
        self.assertIsNone(self.hook('session-end', reason='other'))
        self.assertNotIn('claude-1', hub.sched.sessions)

    def test_heavy_command_waits_for_a_grant_and_runs_under_it(self):
        hub = self.start()
        self.assertIsNone(self.bash('ls -la'))   # light: untouched, nothing in the hub
        self.assertEqual(hub.sched.runs, {})
        line = "npm run build -- --name 'it''s'"
        out = self.bash(line, run_in_background=True)['hookSpecificOutput']
        self.assertNotIn('permissionDecision', out)   # no rule says anything: Claude Code asks as usual
        new = out['updatedInput']
        self.assertEqual((new['description'], new['timeout'], new['run_in_background']), ('d', 600000, True))
        words = shlex.split(new['command'].split(' run ', 1)[1])
        self.assertEqual(words[-2:], ['--shell', line])
        rid = int(words[words.index('--grant') + 1])
        self.assertEqual(words[words.index('--session') + 1], 'claude-1')
        run = hub.sched.runs[rid]
        self.assertEqual((run.state, run.session, run.kind, run.hold, run.notify), ('granted', 'claude-1', 'build',
                                                                                  claude.HOLD, True))
        self.assertEqual(hub.sched.sessions['claude-1'].agent, 'claude-code')   # registered on the way
        time.sleep(0.3)
        self.assertEqual(run.state, 'granted')   # the hook left, the grant waits for the Bash call
        with mock.patch('agenthub.system.can_make_scopes', return_value=False), mock.patch('agenthub.client.say'):
            code = client.cmd_run(argparse.Namespace(argv=[], shell='exit 3', session='claude-1', estimate=None,
                                                     wait=None, kind=None, pool=None, grant=rid))
        self.assertEqual(code, 3)
        self.assertEqual((run.state, run.exit), ('ended', 3))

    def test_permission_rules_of_the_written_command(self):
        hub = self.start()
        self.settings(deny=['Bash(make deploy:*)'], ask=['Bash(cargo build:*)'], allow=['Bash(npm test:*)'])
        self.assertIsNone(self.bash('make deploy'))   # Claude Code's deny rule must see it as written
        self.assertEqual(hub.sched.runs, {})
        self.assertEqual(self.bash('cargo build')['hookSpecificOutput']['permissionDecision'], 'ask')
        self.assertEqual(self.bash('npm test -- -x')['hookSpecificOutput']['permissionDecision'], 'allow')
        self.settings(allow=['Bash(agenthub run:*)'])   # allows the wrapper as such, not every heavy command
        with mock.patch('agenthub.claude.launcher', return_value='agenthub'):
            self.assertEqual(self.bash('cargo build')['hookSpecificOutput']['permissionDecision'], 'ask')
        cli = {'allow': [], 'deny': ['Bash(cargo build:*)'], 'settings': [], 'sources': None}
        with mock.patch('agenthub.claude.cli_options', return_value=cli):   # `claude --disallowedTools …`
            self.assertIsNone(self.bash('cargo build'))
        with open(os.path.join(self.project, '.claude', 'settings.json'), 'w') as f:
            f.write('{broken')
        runs = len(hub.sched.runs)
        self.assertIsNone(self.bash('npm test'))   # unreadable rules: a deny rule may be among them
        self.assertEqual(len(hub.sched.runs), runs)

    def test_wait_running_out_denies_then_room_is_news(self):
        hub = self.start()
        holder = self.w.conn()
        self.addCleanup(holder.close)
        holder.call({'op': 'session.start', 'session': 'other', 'cwd': '/tmp'})
        grant = holder.call({'op': 'run.request', 'session': 'other', 'cmd': 'big', 'cwd': '/tmp', 'estimate': '6500M'})
        self.w.scope('p2', grant['run'])
        holder.send({'op': 'run.started', 'run': grant['run'], 'unit': grant['unit'], 'pid': os.getpid()})
        self.assertTrue(until(lambda: hub.sched.runs[grant['run']].state == 'running'))
        with mock.patch('agenthub.claude.WAIT', 0.5):
            out = self.bash('npm run build')['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertRegex(out['permissionDecisionReason'],
                         r'^agenthub: waited \d+s: needs 2\.0 GiB, .* above reserve; retry later or run it '
                         r'in the background$')
        self.assertIsNone(self.hook('post-tool-use', tool_name='Bash'))   # no news while the room is taken
        self.w.end_scope('p2', grant['run'])
        news = os.path.join(self.w.state, 'news', 'claude-1')
        self.assertTrue(until(lambda: os.path.exists(news)))
        out = self.hook('post-tool-use', tool_name='Read')['hookSpecificOutput']
        self.assertEqual(out['additionalContext'],
                         'agenthub: room for `npm run build` (2.0 GiB) is free now — retry it')
        self.assertFalse(os.path.exists(news))
        self.assertIsNone(self.hook('post-tool-use', tool_name='Read'))   # told once

    def test_refused_command_is_denied(self):
        self.w.cfg.agent_budget = 1000 * M   # a build's 2 GiB never fits
        self.start()
        out = self.bash('npm run build')['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertRegex(out['permissionDecisionReason'], r'^agenthub: refused — .*2\.0 GiB')

    def config(self, text):
        path = os.path.join(self.w.dir, 'cfg.toml')
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_fails_open(self):
        self.assertIsNone(self.bash('npm run build'))   # no hub at all
        self.assertIsNone(self.hook('session-start', source='startup'))
        self.assertIsNone(self.hook('session-end', reason='other'))
        self.assertIsNone(self.hook('post-tool-use', tool_name='Bash'))
        with mock.patch.dict(os.environ, {'AGENTHUB_CONFIG': self.config('[machine\n')}):
            self.assertIsNone(self.bash('npm run build'))   # a broken config
        out = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO('not json')), contextlib.redirect_stdout(out):
            self.assertEqual(claude.main('pre-tool-use'), 0)
        self.assertEqual(out.getvalue(), '')

    def test_already_through_the_hub_or_plan_mode_is_left_alone(self):
        hub = self.start()
        self.assertIsNone(self.bash('agenthub run -- make'))
        self.assertIsNone(self.hook('pre-tool-use', tool_name='Bash', permission_mode='plan',
                                    tool_input={'command': 'make'}))
        self.assertIsNone(self.hook('pre-tool-use', tool_name='Edit', tool_input={'file_path': 'x'}))
        self.assertEqual(hub.sched.runs, {})

    def test_status_line(self):
        out = io.StringIO()
        args = argparse.Namespace(line=True, json=False, session='claude-1')
        with contextlib.redirect_stdout(out):
            self.assertEqual(client.cmd_status(args), 0)   # no hub: nothing, and no start through the socket
        self.assertEqual(out.getvalue(), '')
        self.assertFalse(system.hub_running(self.w.sock))
        self.start()
        self.assertTrue(system.hub_running(self.w.sock))
        self.hook('session-start', source='startup')
        with mock.patch('agenthub.system.can_make_scopes', return_value=False):
            self.bash('npm test')
        with contextlib.redirect_stdout(out):
            self.assertEqual(client.cmd_status(args), 0)
        self.assertEqual(out.getvalue(), 'hub ▸ room 5.3 GiB · you p2 · 1 running\n')   # 7000M budget − 1.5 GiB

    def test_status_line_takes_the_session_from_claude_code_and_never_hangs(self):
        self.start()
        self.hook('session-start', source='startup')
        cli = [sys.executable, '-P', '-m', 'agenthub', 'status', '--line']
        env = dict(os.environ, PYTHONPATH=system.ROOT)
        out = subprocess.run(cli, input='{"session_id": "claude-1", "model": {}}', capture_output=True, text=True,
                             env=env, timeout=10)
        self.assertEqual(out.stdout, 'hub ▸ room 6.8 GiB · you p2\n')
        with subprocess.Popen(cli, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env) as proc:
            start = time.monotonic()   # a caller that never closes the pipe
            line = proc.stdout.readline()
            self.assertLess(time.monotonic() - start, 5)
            proc.stdin.close()
        self.assertEqual(line, 'hub ▸ room 6.8 GiB\n')

    def test_launcher_is_this_package(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'agenthub')
            direct = shlex.join(system.self_command())
            with mock.patch.dict(os.environ, {'PATH': d}):
                self.assertEqual(claude.launcher(), direct)   # nothing on PATH
                for text, expect in ((f'#!/bin/sh\nexec {direct} "$@"\n', 'agenthub'),   # what install writes
                                     ('#!/bin/sh\nPYTHONPATH=/old/checkout exec python3 -m agenthub', direct)):
                    with open(path, 'w') as f:
                        f.write(text)
                    os.chmod(path, 0o755)
                    self.assertEqual(claude.launcher(), expect, text)

    def test_a_silent_hub_does_not_hold_the_hook_past_its_limit(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.w.sock)
        listener.listen()
        self.addCleanup(listener.close)

        def stall():   # answers once, then says nothing more
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(b'{"ok": true, "state": "queued", "run": 1, "position": 1}\n')
            time.sleep(3)
            conn.close()
        thread = threading.Thread(target=stall, daemon=True)
        thread.start()
        start = time.monotonic()
        with mock.patch('agenthub.claude.WAIT', -20):
            self.assertIsNone(self.bash('npm test'))   # the call runs as written
        self.assertLess(time.monotonic() - start, 2.5)


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.cleanup)
        self.hub, thread = self.w.start()
        self.addCleanup(self.w.stop, self.hub, thread)
        for sid in ('a', 'b'):
            self.w.call({'op': 'session.start', 'session': sid, 'cwd': '/tmp'})

    def granted(self, **extra):
        conn = self.w.conn()
        grant = conn.call(dict({'op': 'run.request', 'session': 'a', 'cmd': 'x', 'cwd': '/tmp', 'estimate': '1M'},
                               **extra))
        conn.close()
        return grant['run']

    def test_claim_rules(self):
        rid = self.granted(hold=60)
        self.assertEqual(self.w.call({'op': 'run.claim', 'run': rid, 'session': 'b'})['error']['code'], 'grant_gone')
        mine = self.w.conn()
        self.addCleanup(mine.close)
        self.assertEqual(mine.call({'op': 'run.claim', 'run': rid, 'session': 'a'})['state'], 'granted')
        self.assertEqual(self.w.call({'op': 'run.claim', 'run': rid, 'session': 'a'})['error']['message'],
                         f'run {rid} already has a client')
        self.assertEqual(self.w.call({'op': 'run.claim', 'run': 999, 'session': 'a'})['error']['code'], 'unknown_run')
        self.assertEqual(self.w.call({'op': 'run.claim', 'run': rid})['error']['code'], 'bad_request')   # whose?

    def test_hold_keeps_a_grant_only_that_long(self):
        rid = self.granted(hold='0.3s')
        self.assertTrue(until(lambda: self.hub.sched.runs[rid].state == 'cancelled', timeout=3))
        self.assertEqual(self.w.call({'op': 'run.claim', 'run': rid, 'session': 'a'})['error']['message'],
                         f'run {rid} is cancelled')
        for bad in ({'hold': 0}, {'hold': 601}, {'hold': 'soon'}, {'notify': 'yes'}):
            conn = self.w.conn()
            answer = conn.call(dict({'op': 'run.request', 'session': 'a', 'cmd': 'x', 'estimate': '1M'}, **bad))
            conn.close()
            self.assertEqual(answer['error']['code'], 'bad_request', bad)

    def test_a_gone_grant_queues_again(self):
        rid = self.granted(hold='0.2s')
        self.assertTrue(until(lambda: self.hub.sched.runs[rid].state == 'cancelled', timeout=3))
        with mock.patch.dict(os.environ, {'AGENTHUB_SOCKET': self.w.sock, 'AGENTHUB_PREFIX': 'agenthubtest'}), \
                mock.patch('agenthub.system.can_make_scopes', return_value=False), \
                mock.patch('agenthub.client.say') as said:
            code = client.cmd_run(argparse.Namespace(argv=[], shell='exit 4', session='a', estimate='1M', wait=None,
                                                     kind=None, pool=None, grant=rid))
        self.assertEqual(code, 4)
        self.assertIn(f'grant {rid} is gone', said.call_args_list[0][0][0])
        self.assertEqual(self.hub.sched.runs[rid].state, 'cancelled')   # the new request is another run


if __name__ == '__main__':
    unittest.main()
