"""`agenthub mcp` as an MCP client meets it: JSON-RPC lines over its stdin and stdout, a real hub over its socket
(fake cgroups and memory), and no systemd scopes ($XDG_RUNTIME_DIR points at a temp dir)."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest

from tests.test_hub import M, World, until

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpTest(unittest.TestCase):
    def setUp(self):
        self.w = World(budget=500 * M)
        self.w.cfg.default_estimate = 100 * M
        self.addCleanup(self.w.cleanup)
        self.hub, thread = self.w.start()
        self.addCleanup(self.w.stop, self.hub, thread)
        self.runtime = os.path.join(self.w.dir, 'runtime')
        os.makedirs(self.runtime)
        self.env = dict(os.environ, AGENTHUB_SOCKET=self.w.sock, AGENTHUB_PREFIX='agenthubtest', PYTHONPATH=ROOT,
                        AGENTHUB_CONFIG=os.path.join(self.w.dir, 'none.toml'), XDG_RUNTIME_DIR=self.runtime)
        for name in ('AGENTHUB_SESSION', 'AGENTHUB_RUN', 'AGENTHUB_SESSION_PID', 'AGENTHUB_AGENT'):
            self.env.pop(name, None)

    def server(self, **env):
        proc = subprocess.Popen([sys.executable, '-P', '-m', 'agenthub', 'mcp'], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(self.env, **env),
                                cwd=self.w.dir)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):   # closed last, after the server is gone
            self.addCleanup(pipe.close)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        inbox = queue.Queue()

        def read():
            for line in proc.stdout:
                inbox.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        self.proc, self.inbox, self.next_id = proc, inbox, 0
        return proc

    def send(self, method, params=None, notify=False):
        msg = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notify:
            self.next_id += 1
            msg['id'] = self.next_id
        self.proc.stdin.write((json.dumps(msg) + '\n').encode())
        self.proc.stdin.flush()
        return msg.get('id')

    def answer(self, mid, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.inbox.get(timeout=deadline - time.monotonic())
            if msg.get('id') == mid:
                return msg
        raise AssertionError(f'no answer to {mid}')

    def call(self, method, params=None):
        return self.answer(self.send(method, params))

    def initialize(self, name='Codex CLI'):
        out = self.call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                       'clientInfo': {'name': name, 'version': '1'}})
        self.send('notifications/initialized', notify=True)
        return out

    def test_handshake_tools_and_a_run(self):
        self.server()
        init = self.initialize()['result']
        self.assertEqual((init['protocolVersion'], init['serverInfo']['name']), ('2025-06-18', 'agenthub'))
        self.assertIn('tools', init['capabilities'])
        session = next(iter(self.hub.sched.sessions.values()))
        self.assertRegex(session.id, r'^mcp-Codex-CLI-[0-9a-f]{12}$')
        self.assertEqual((session.agent, session.pid), ('Codex-CLI', os.getpid()))   # the client that started it
        tools = self.call('tools/list')['result']['tools']
        self.assertEqual([t['name'] for t in tools], ['run'])
        self.assertEqual(tools[0]['inputSchema']['required'], ['command'])
        out = self.call('tools/call', {'name': 'run', 'arguments': {
            'command': 'echo "run=$AGENTHUB_RUN"; read x; echo "stdin:${x:-empty}"; pwd; echo oops >&2; exit 4',
            'cwd': self.runtime}})['result']
        run = next(iter(self.hub.sched.runs.values()))
        self.assertEqual((run.session, run.state, run.exit), (session.id, 'ended', 4))
        self.assertFalse(out['isError'])
        self.assertEqual(out['content'][0]['text'], f'exit 4\nrun={run.id}\nstdin:empty\n{self.runtime}\noops\n')
        self.assertEqual(self.call('ping')['result'], {})
        self.assertEqual(self.call('nope')['error']['code'], -32601)
        self.assertEqual(self.call('tools/call', {'name': 'shell', 'arguments': {}})['error']['code'], -32602)
        bad = self.call('tools/call', {'name': 'run', 'arguments': {'command': ' '}})['result']
        self.assertTrue(bad['isError'])
        self.assertTrue(self.call('tools/call', {'name': 'run', 'arguments': {'command': 'true',
                                                                              'cwd': '/nowhere'}})['result']['isError'])
        self.proc.stdin.close()   # the client went away: the session goes with it
        self.assertEqual(self.proc.wait(10), 0)
        self.assertNotIn(session.id, self.hub.sched.sessions)

    def test_long_output_is_cut_and_a_wait_shows_in_it(self):
        self.server()
        self.initialize()
        holder = self.w.conn()
        self.addCleanup(holder.close)
        holder.call({'op': 'session.start', 'session': 'other', 'cwd': '/tmp'})
        grant = holder.call({'op': 'run.request', 'session': 'other', 'cmd': 'big', 'cwd': '/tmp', 'estimate': '450M'})
        holder.send({'op': 'run.started', 'run': grant['run'], 'unit': None, 'pid': os.getpid()})
        mid = self.send('tools/call', {'name': 'run', 'arguments': {'command': 'seq 1 5000'}})
        self.assertTrue(until(lambda: any(r.state == 'queued' for r in self.hub.sched.runs.values())))
        holder.send({'op': 'run.ended', 'run': grant['run'], 'exit': 0})
        text = self.answer(mid)['result']['content'][0]['text']
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith('exit 0; '), lines[0])
        self.assertEqual(lines[-1], '5000')
        self.assertLessEqual(len(lines), 201)
        self.assertLessEqual(len(text), 17 << 10)

    def test_output_that_outlives_the_command_or_has_no_lines(self):
        self.server()
        self.initialize()
        start = time.monotonic()
        out = self.call('tools/call', {'name': 'run', 'arguments': {'command': 'sleep 5 & echo started'}})['result']
        self.assertLess(time.monotonic() - start, 4)   # the child holding the pipe does not hold the answer
        self.assertEqual(out['content'][0]['text'], 'exit 0\nstarted\n')
        out = self.call('tools/call', {'name': 'run', 'arguments': {
            'command': "head -c 40000 /dev/zero | tr '\\0' x; printf 'END'"}})['result']
        text = out['content'][0]['text']
        self.assertTrue(text.startswith('exit 0\n') and text.endswith('xEND'), text[:40])
        self.assertLessEqual(len(text), (16 << 10) + 10)

    def test_cancel_takes_the_run_out_of_the_queue(self):
        self.server()
        self.initialize()
        holder = self.w.conn()
        self.addCleanup(holder.close)
        holder.call({'op': 'session.start', 'session': 'other', 'cwd': '/tmp'})
        holder.call({'op': 'run.request', 'session': 'other', 'cmd': 'big', 'cwd': '/tmp', 'estimate': '450M'})
        mid = self.send('tools/call', {'name': 'run', 'arguments': {'command': 'echo should-not-run'}})
        self.assertTrue(until(lambda: any(r.state == 'queued' and r.cmd == 'echo should-not-run'
                                          for r in self.hub.sched.runs.values())))
        self.send('notifications/cancelled', {'requestId': mid, 'reason': 'user'}, notify=True)
        self.assertTrue(until(lambda: all(r.state != 'queued' for r in self.hub.sched.runs.values()
                                          if r.cmd == 'echo should-not-run')))
        self.assertEqual(self.call('ping')['result'], {})   # and the cancelled call got no answer
        self.assertTrue(self.inbox.empty())

    def test_under_wrap_and_without_hub(self):
        # under `agenthub wrap` the wrapper's session is used, not a second one
        self.server(AGENTHUB_SESSION='codex-1', AGENTHUB_AGENT='codex', AGENTHUB_SESSION_PID=str(os.getpid()))
        self.initialize()
        out = self.call('tools/call', {'name': 'run', 'arguments': {'command': 'echo hi'}})['result']
        self.assertEqual(out['content'][0]['text'], 'exit 0\nhi\n')
        self.assertEqual(list(self.hub.sched.sessions), ['codex-1'])
        self.assertEqual(self.hub.sched.sessions['codex-1'].agent, 'codex')
        self.proc.stdin.close()
        self.proc.wait(10)
        self.assertIn('codex-1', self.hub.sched.sessions)   # not ours to end
        # no hub: the command still runs, and says so
        self.server(AGENTHUB_SOCKET=os.path.join(self.w.dir, 'none.sock'))
        self.initialize()
        out = self.call('tools/call', {'name': 'run', 'arguments': {'command': 'echo hi'}})['result']
        self.assertIn('running without the hub', out['content'][0]['text'])
        self.assertTrue(out['content'][0]['text'].endswith('hi\n'))
        self.assertTrue(self.call('tools/call', {'name': 'run', 'arguments': 'echo'})['result']['isError'])
        self.send('ping', notify=True)   # a request without an id is a notification: no answer
        self.assertEqual(self.call('ping')['result'], {})
        self.assertTrue(self.inbox.empty())
        self.proc.stdin.write(b'not json\n[1]\n')
        self.proc.stdin.flush()
        self.assertEqual([self.inbox.get(timeout=5)['error']['code'] for _ in range(2)], [-32700, -32600])


if __name__ == '__main__':
    unittest.main()
