"""The hub over its real socket protocol, with fake memory readings and a fake cgroup tree in a temp dir."""
import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from agenthub import client, config as c, system
from agenthub.hub import Hub, listening_socket
from agenthub.scheduler import Machine

M = 1 << 20
PFX = 'agenthubtest'


class World:
    """A temp dir holding the socket, the state dir and a fake cgroup tree."""

    def __init__(self, budget=1000 * M):
        self.dir = tempfile.mkdtemp(prefix='agenthub-test-')
        self.sock = os.path.join(self.dir, 'hub.sock')
        self.state = os.path.join(self.dir, 'state')
        self.cfg = c.build({'machine': {'agent_budget': budget, 'desktop_reserve': 0, 'idle_exit': '1h'}}, 8000 * M)
        self.available = 8000 * M
        self.stopped = []

    def cgroups(self):
        cg = system.Cgroups(PFX, base='/')
        cg.base = os.path.join(self.dir, 'cg', f'{PFX}.slice')
        return cg

    def scope(self, cls, rid, populated=True):
        d = os.path.join(self.dir, 'cg', f'{PFX}.slice', f'{PFX}-{cls}.slice', f'{PFX}-run-{rid}.scope')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'cgroup.events'), 'w') as f:
            f.write(f'populated {int(populated)}\nfrozen 0\n')
        with open(os.path.join(d, 'memory.current'), 'w') as f:
            f.write(f'{10 * M}\n')
        with open(os.path.join(d, 'memory.peak'), 'w') as f:
            f.write(f'{20 * M}\n')
        return d

    def machine(self):
        return Machine(self.available, 0, {})

    def start(self, tick=0.1):
        hub = Hub(self.cfg, PFX, self.cgroups(), self.state, stop_unit=self.stopped.append,
                  read_machine=self.machine)
        hub.tick_interval = tick
        sock = listening_socket(self.sock)
        hub.crashed = None

        def serve():
            try:
                asyncio.run(hub.serve(sock))
            except Exception as e:
                hub.crashed = e
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while hub.server is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return hub, thread

    def stop(self, hub, thread):
        if thread.is_alive():
            hub.loop.call_soon_threadsafe(lambda: hub.done.done() or hub.done.set_result(None))
        thread.join(5)

    def conn(self):
        return client.Conn(self.sock)

    def call(self, msg):
        conn = self.conn()
        try:
            return conn.call(msg)
        finally:
            conn.close()

    def end_scope(self, cls, rid):
        with open(os.path.join(self.scope(cls, rid), 'cgroup.events'), 'w') as f:
            f.write('populated 0\nfrozen 0\n')

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class HubTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.cleanup)
        self.hub, self.thread = self.w.start()
        self.addCleanup(self.w.stop, self.hub, self.thread)
        admin = self.w.conn()
        self.addCleanup(admin.close)
        for sid, cls in (('a', 'p1'), ('z', 'p3')):
            self.assertTrue(admin.call({'op': 'session.start', 'session': sid, 'agent': 'test', 'cwd': '/tmp'})['ok'])
            self.assertEqual(admin.call({'op': 'session.prio', 'session': sid, 'class': cls})['class'], cls)
        self.admin = admin

    def request(self, sid, estimate, **extra):
        conn = self.w.conn()
        self.addCleanup(conn.close)
        conn.send(dict({'op': 'run.request', 'session': sid, 'cmd': 'sleep 1', 'cwd': '/tmp', 'estimate': estimate},
                       **extra))
        return conn, conn.recv()

    def test_grant_queue_and_release_on_scope_end(self):
        c1, first = self.request('a', '600M')
        self.assertEqual((first['state'], first['slice'], first['unit']),
                         ('granted', f'{PFX}-p1.slice', f'{PFX}-run-{first["run"]}.scope'))
        c2, second = self.request('z', '600M')
        self.assertEqual(second['state'], 'queued')
        self.assertIn('needs 600 MiB, 400 MiB above reserve', second['reason'])
        status = self.admin.call({'op': 'status'})
        self.assertEqual([q['id'] for q in status['queue']], [second['run']])
        scope = self.w.scope('p1', first['run'])
        c1.send({'op': 'run.started', 'run': first['run'], 'unit': first['unit'], 'pid': os.getpid()})
        with open(os.path.join(scope, 'cgroup.events'), 'w') as f:
            f.write('populated 0\nfrozen 0\n')
        granted = c2.recv()
        self.assertEqual((granted['state'], granted['run']), ('granted', second['run']))
        ended = c1.call({'op': 'run.ended', 'run': first['run'], 'exit': 0})
        self.assertEqual((ended['ok'], ended['peak']), (True, 20 * M))

    def test_leaving_the_queue_cancels(self):
        self.request('a', '900M')
        c2, queued = self.request('z', '500M')
        self.assertEqual(queued['state'], 'queued')
        c2.close()
        self.assertTrue(until(lambda: self.hub.sched.runs[queued['run']].state == 'cancelled'))

    def test_errors(self):
        _, unknown = self.request('nobody', '1M')
        self.assertEqual(unknown['error']['code'], 'unknown_session')
        _, refused = self.request('a', '5G')
        self.assertEqual(refused['error']['code'], 'refused')
        self.assertEqual(self.admin.call({'op': 'warp'})['error']['code'], 'bad_request')
        self.admin.sock.sendall(b'{"v": 2, "op": "status"}\n')
        self.assertEqual(self.admin.recv()['error']['code'], 'version')
        self.admin.sock.sendall(b'not json\n')
        self.assertEqual(self.admin.recv()['error']['code'], 'bad_request')
        self.assertEqual(self.admin.call({'op': 'run.ended', 'run': 999})['error']['code'], 'unknown_run')

    def test_wait_expires(self):
        self.request('a', '900M')
        conn, queued = self.request('z', '500M', wait='0.3s')
        self.assertEqual(queued['state'], 'queued')
        self.assertEqual(conn.recv()['error']['code'], 'wait_expired')

    def test_stop_running_run(self):
        conn, grant = self.request('a', '100M')
        self.w.scope('p1', grant['run'])
        conn.send({'op': 'run.started', 'run': grant['run'], 'unit': grant['unit'], 'pid': os.getpid()})
        answer = self.admin.call({'op': 'run.stop', 'run': grant['run'], 'reason': 'make room'})
        self.assertEqual(answer['state'], 'stopping')
        self.assertEqual(conn.recv(), {'ok': True, 'state': 'stopping', 'run': grant['run'], 'reason': 'make room'})
        self.assertTrue(until(lambda: self.w.stopped == [grant['unit']]))
        self.assertEqual(conn.call({'op': 'run.ended', 'run': grant['run'], 'exit': 143})['stopped'], 'make room')

    def test_stop_while_the_scope_comes_up_is_repeated_once_it_runs(self):
        conn, grant = self.request('a', '100M')
        self.assertEqual(self.admin.call({'op': 'run.stop', 'run': grant['run']})['state'], 'stopping')
        self.w.scope('p1', grant['run'])
        conn.send({'op': 'run.started', 'run': grant['run'], 'unit': grant['unit'], 'pid': os.getpid()})
        self.assertTrue(until(lambda: self.w.stopped == [grant['unit'], grant['unit']]))

    def test_run_without_scope_lives_as_long_as_its_connection(self):
        conn, grant = self.request('a', '100M')
        conn.send({'op': 'run.started', 'run': grant['run'], 'unit': None, 'pid': os.getpid()})
        time.sleep(0.3)  # several ticks: no scope must not mean "ended"
        self.assertEqual(self.hub.sched.runs[grant['run']].state, 'running')
        conn.close()
        self.assertTrue(until(lambda: self.hub.sched.runs[grant['run']].state == 'ended'))

    def test_slow_scope_is_not_taken_for_an_ended_run(self):
        conn, grant = self.request('a', '100M')
        conn.send({'op': 'run.started', 'run': grant['run'], 'unit': grant['unit'], 'pid': os.getpid()})
        time.sleep(0.3)
        self.assertEqual(self.hub.sched.runs[grant['run']].state, 'running')
        self.w.scope('p1', grant['run'])
        time.sleep(0.3)
        self.w.end_scope('p1', grant['run'])
        self.assertTrue(until(lambda: self.hub.sched.runs[grant['run']].state == 'ended'))

    def test_bad_values_are_bad_requests(self):
        for estimate in ('lots', '15%', -1):
            _, answer = self.request('a', estimate)
            self.assertEqual(answer['error']['code'], 'bad_request', estimate)
        answer = self.admin.call({'op': 'session.start', 'session': 'x', 'pid': 'abc', 'cwd': '/tmp'})
        self.assertEqual(answer['error']['code'], 'bad_request')
        answer = self.admin.call({'op': 'session.start', 'session': 'x', 'pid': str(os.getpid()), 'cwd': '/tmp'})
        self.assertEqual((answer['ok'], self.hub.sched.sessions['x'].pid), (True, os.getpid()))
        status = self.admin.call({'op': 'status'})
        self.assertEqual({s['id']: s['class'] for s in status['sessions']}, {'a': 'p1', 'z': 'p3', 'x': 'p2'})

    def test_dead_session_drops_its_queue(self):
        proc = subprocess.Popen(['sleep', '30'])
        self.addCleanup(proc.kill)
        self.assertTrue(self.admin.call({'op': 'session.start', 'session': 'mortal', 'pid': proc.pid,
                                         'cwd': '/tmp'})['ok'])
        self.request('a', '900M')
        conn, queued = self.request('mortal', '500M')
        self.assertEqual(queued['state'], 'queued')
        proc.kill()
        proc.wait()
        self.assertEqual(conn.recv()['error']['code'], 'cancelled')
        self.assertNotIn('mortal', self.hub.sched.sessions)

    def test_session_with_missing_process_is_refused(self):
        answer = self.admin.call({'op': 'session.start', 'session': 'ghost', 'pid': 2 ** 22 - 1, 'cwd': '/tmp'})
        self.assertEqual(answer['error']['code'], 'bad_request')

    def test_session_id_cannot_leave_the_state_dir(self):
        outside = os.path.join(self.w.dir, 'outside.json')
        with open(outside, 'w') as f:
            f.write('{}')
        for op in ('session.start', 'session.end', 'session.prio'):
            answer = self.admin.call({'op': op, 'session': '../../outside', 'class': 'p1', 'cwd': '/tmp'})
            self.assertEqual(answer['error']['code'], 'bad_request', op)
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(os.listdir(self.w.state), ['sessions'])

    def test_client_dying_after_scope_start_is_still_accounted(self):
        conn, grant = self.request('a', '100M')
        self.w.scope('p1', grant['run'])
        conn.close()
        self.assertTrue(until(lambda: self.hub.sched.runs[grant['run']].state == 'running'))

    def test_tick_failure_ends_the_hub(self):
        with mock.patch.object(self.hub.sched, 'expire', side_effect=RuntimeError('boom')), \
                mock.patch('agenthub.hub.log'):
            self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(str(self.hub.crashed), 'boom')


class RestartTest(unittest.TestCase):
    def test_state_survives_restart(self):
        w = World()
        self.addCleanup(w.cleanup)
        hub, thread = w.start()
        conn = w.conn()
        conn.call({'op': 'session.start', 'session': 'a', 'pid': os.getpid(), 'cwd': '/tmp'})
        conn.call({'op': 'session.prio', 'session': 'a', 'class': 'p0'})
        conn.send({'op': 'run.request', 'session': 'a', 'cmd': 'long', 'cwd': '/tmp', 'estimate': '300M'})
        grant = conn.recv()
        w.scope('p0', grant['run'])
        w.scope('p3', 77)  # a run whose metadata is lost: still accounted
        conn.send({'op': 'run.started', 'run': grant['run'], 'unit': grant['unit'], 'pid': os.getpid()})
        self.assertTrue(until(lambda: hub.sched.runs[grant['run']].state == 'running'))
        conn.close()
        w.stop(hub, thread)

        hub2, thread2 = w.start()
        self.addCleanup(w.stop, hub2, thread2)
        st = w.call({'op': 'status'})
        self.assertEqual([(s['id'], s['class'], s['source']) for s in st['sessions']], [('a', 'p0', 'override')])
        runs = {r['id']: r for r in st['runs']}
        self.assertEqual((runs[grant['run']]['cmd'], runs[grant['run']]['estimate']), ('long', 300 * M))
        self.assertIn(77, runs)
        self.assertGreater(hub2.sched.next_id, 77)
        answer = w.call({'op': 'run.ended', 'run': grant['run'], 'exit': 0})
        self.assertTrue(answer['ok'])

    def test_grant_given_before_restart_is_adopted_when_its_scope_runs(self):
        w = World()
        self.addCleanup(w.cleanup)
        hub, thread = w.start()
        conn = w.conn()
        conn.call({'op': 'session.start', 'session': 'a', 'pid': 0, 'cwd': '/tmp'})
        conn.send({'op': 'run.request', 'session': 'a', 'cmd': 'slow start', 'cwd': '/tmp', 'estimate': '300M'})
        grant = conn.recv()
        w.stop(hub, thread)
        conn.close()
        hub2, thread2 = w.start()
        self.addCleanup(w.stop, hub2, thread2)
        self.assertEqual(hub2.sched.runs[grant['run']].state, 'granted')
        w.scope('p2', grant['run'])
        self.assertTrue(until(lambda: hub2.sched.runs[grant['run']].state == 'running'))


class InotifyTest(unittest.TestCase):
    def test_scope_end_is_seen_without_the_tick(self):
        w = World()
        self.addCleanup(w.cleanup)
        hub, thread = w.start(tick=60)
        self.addCleanup(w.stop, hub, thread)
        conn = w.conn()
        self.addCleanup(conn.close)
        conn.call({'op': 'session.start', 'session': 'a', 'pid': 0, 'cwd': '/tmp'})
        conn.send({'op': 'run.request', 'session': 'a', 'cmd': 'x', 'cwd': '/tmp', 'estimate': '1M'})
        grant = conn.recv()
        w.scope('p2', grant['run'])
        conn.send({'op': 'run.started', 'run': grant['run'], 'unit': grant['unit'], 'pid': os.getpid()})
        self.assertTrue(until(lambda: hub.sched.runs[grant['run']].state == 'running'))
        w.end_scope('p2', grant['run'])
        self.assertTrue(until(lambda: hub.sched.runs[grant['run']].state == 'ended', timeout=3))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.cleanup)
        env = mock.patch.dict(os.environ, {'AGENTHUB_SOCKET': self.w.sock, 'AGENTHUB_PREFIX': PFX,
                                           'AGENTHUB_CONFIG': os.path.join(self.w.dir, 'none.toml')})
        env.start()
        self.addCleanup(env.stop)
        nosystemd = mock.patch('agenthub.system.can_make_scopes', return_value=False)
        nosystemd.start()
        self.addCleanup(nosystemd.stop)

    def args(self, *argv, **kw):
        return argparse.Namespace(argv=list(argv), shell=kw.get('shell'), session=kw.get('session'),
                                  estimate=kw.get('estimate', '10M'), wait=kw.get('wait'))

    def test_run_through_hub_registers_implicit_session_and_returns_exit_code(self):
        hub, thread = self.w.start()
        self.addCleanup(self.w.stop, hub, thread)
        self.assertEqual(client.cmd_run(self.args('--', 'sh', '-c', 'exit 3')), 3)
        self.assertEqual(client.cmd_run(self.args(shell='exit 4')), 4)
        ended = [r for r in hub.sched.runs.values() if r.state == 'ended']
        self.assertEqual(sorted(r.exit for r in ended), [3, 4])
        self.assertIn(f'pid-{os.getppid()}', hub.sched.sessions)

    def test_fails_open_without_hub(self):
        with mock.patch('agenthub.client.say') as said:
            self.assertEqual(client.cmd_run(self.args('--', 'sh', '-c', 'exit 5')), 5)
        self.assertIn('running without the hub', said.call_args[0][0])

    def test_refused_and_expired_exit_codes(self):
        hub, thread = self.w.start()
        self.addCleanup(self.w.stop, hub, thread)
        with mock.patch('agenthub.client.say'):
            self.assertEqual(client.cmd_run(self.args('true', estimate='5G')), client.EX_UNAVAILABLE)
            hold = self.w.conn()
            self.addCleanup(hold.close)
            hold.call({'op': 'session.start', 'session': 'x', 'cwd': '/tmp'})
            hold.send({'op': 'run.request', 'session': 'x', 'cmd': 'hold', 'cwd': '/tmp', 'estimate': '900M'})
            hold.recv()
            self.assertEqual(client.cmd_run(self.args('true', estimate='500M', wait='0.3s')), client.EX_TEMPFAIL)

    def test_nothing_to_run_and_bad_values(self):
        with mock.patch('agenthub.client.say'):
            self.assertEqual(client.cmd_run(self.args()), 2)
            self.assertEqual(client.cmd_run(self.args('true', estimate='lots')), 2)
            self.assertEqual(client.cmd_run(self.args('true', wait='soon')), 2)

    def test_silent_hub_fails_open(self):
        mute = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(mute.close)
        mute.bind(self.w.sock)
        mute.listen(4)  # accepts into the backlog, never answers: a crash-looping socket-activated hub
        with mock.patch('agenthub.client.ANSWER_TIMEOUT', 0.3), mock.patch('agenthub.client.say') as said:
            self.assertEqual(client.cmd_run(self.args('--', 'sh', '-c', 'exit 6')), 6)
        self.assertIn('did not answer', said.call_args[0][0])

    def test_run_is_held_while_the_command_runs(self):
        hub, thread = self.w.start()
        self.addCleanup(self.w.stop, hub, thread)
        seen = []

        def watch():  # signal handlers need the main thread, so the command runs there and this one looks
            until(lambda: any(r.state == 'running' for r in hub.sched.runs.values()))
            time.sleep(0.3)  # several ticks later the hub must still hold it
            seen.append([r.state for r in hub.sched.runs.values()])
        watcher = threading.Thread(target=watch)
        watcher.start()
        self.assertEqual(client.cmd_run(self.args('sleep', '0.8')), 0)
        watcher.join(5)
        self.assertEqual(seen, [['running']])
        self.assertEqual([r.state for r in hub.sched.runs.values()], ['ended'])


if __name__ == '__main__':
    unittest.main()
