import unittest

from agenthub import config as c
from agenthub.scheduler import Machine, Scheduler, SchedulerError

M = 1 << 20


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1
        return self.t


def make(budget=1000 * M, reserve=0):
    cfg = c.build({'machine': {'agent_budget': budget, 'desktop_reserve': reserve}}, 8000 * M)
    s = Scheduler(cfg, Clock())
    for sid, cls in (('a', 'p1'), ('b', 'p2'), ('z', 'p3')):
        s.start_session(sid, 'test', None, '/tmp')
        s.set_class(sid, cls)
    return s


def plenty(**runs):
    return Machine(mem_available=8000 * M, agent_current=0, run_current=runs)


class AdmissionTest(unittest.TestCase):
    def test_fits_then_queues_with_reason(self):
        s = make()
        r1 = s.request('a', 'build', '/tmp', estimate=600 * M)
        r2 = s.request('z', 'test', '/tmp', estimate=600 * M)
        self.assertEqual([r.id for r in s.decide(plenty())], [r1.id])
        self.assertEqual(r2.state, 'queued')
        self.assertIn('needs 600 MiB, 400 MiB above reserve', r2.reason)
        s.finish(r1.id, exit=0)
        self.assertEqual([r.id for r in s.decide(plenty())], [r2.id])

    def test_growth_of_admitted_runs_counts(self):
        s = make()
        r1 = s.request('a', 'x', '/tmp', estimate=600 * M)
        s.decide(plenty())
        r2 = s.request('a', 'y', '/tmp', estimate=300 * M)
        # r1 has used only 100M so far; it will still take 500M more
        machine = Machine(8000 * M, 100 * M, {r1.id: 100 * M})
        self.assertEqual(s.room(machine), (900 * M, 500 * M, 400 * M))
        self.assertEqual([r.id for r in s.decide(machine)], [r2.id])

    def test_reserve_and_budget_both_limit(self):
        s = make(budget=1000 * M, reserve=500 * M)
        tight = Machine(mem_available=700 * M, agent_current=0)
        self.assertEqual(s.room(tight)[0], 200 * M)
        r = s.request('a', 'x', '/tmp', estimate=300 * M)
        s.decide(tight)
        self.assertEqual(r.state, 'queued')

    def test_order_by_class_then_time_and_no_overtaking(self):
        s = make()
        big = s.request('b', 'big', '/tmp', estimate=900 * M)
        small = s.request('z', 'small', '/tmp', estimate=100 * M)
        hold = s.request('a', 'hold', '/tmp', estimate=200 * M)
        self.assertEqual([r.id for r in s.queue()], [hold.id, big.id, small.id])
        self.assertEqual([r.id for r in s.decide(plenty())], [hold.id])
        self.assertEqual(small.state, 'queued')
        self.assertIn(f'behind run {big.id}', small.reason)

    def test_refused_when_it_can_never_fit(self):
        s = make()
        r = s.request('a', 'huge', '/tmp', estimate=2000 * M)
        self.assertEqual(r.state, 'refused')
        self.assertIn('whole agent budget', r.reason)
        self.assertEqual(s.queue(), [])

    def test_unknown_session(self):
        s = make()
        with self.assertRaises(SchedulerError) as e:
            s.request('nobody', 'x', '/tmp')
        self.assertEqual(e.exception.code, 'unknown_session')

    def test_kind_estimate_used_when_none_given(self):
        cfg = c.build({'kinds': [{'name': 'build', 'match': 'tsc', 'estimate': '300M'}]}, 8000 * M)
        s = Scheduler(cfg, Clock())
        s.start_session('a', 't', None, '/tmp')
        r = s.request('a', 'npx tsc', '/tmp')
        self.assertEqual((r.kind, r.estimate), ('build', 300 * M))
        self.assertEqual(r.memory_high, 450 * M)
        self.assertEqual(s.request('a', 'true', '/tmp', estimate=1).memory_high, 256 * M)


class LifecycleTest(unittest.TestCase):
    def test_wait_expires(self):
        s = make()
        s.request('a', 'hold', '/tmp', estimate=900 * M)
        s.decide(plenty())
        r = s.request('z', 'x', '/tmp', estimate=500 * M, wait=3)
        s.decide(plenty())
        self.assertEqual(s.expire(), [])
        s.clock.t += 5
        self.assertEqual([x.id for x in s.expire()], [r.id])
        self.assertEqual(r.state, 'expired')

    def test_session_end_cancels_queued_only(self):
        s = make()
        running = s.request('a', 'run', '/tmp', estimate=900 * M)
        s.decide(plenty())
        s.mark_started(running.id, 'u', 1)
        queued = s.request('a', 'wait', '/tmp', estimate=500 * M)
        cancelled = s.end_session('a')
        self.assertEqual([r.id for r in cancelled], [queued.id])
        self.assertEqual(running.state, 'running')
        self.assertNotIn('a', s.sessions)

    def test_reprioritising_reorders_queue(self):
        s = make()
        s.request('a', 'hold', '/tmp', estimate=900 * M)
        s.decide(plenty())
        first = s.request('b', 'x', '/tmp', estimate=500 * M)
        second = s.request('z', 'y', '/tmp', estimate=500 * M)
        self.assertEqual(s.queue()[0].id, first.id)
        s.set_class('z', 'p0')
        self.assertEqual(s.queue()[0].id, second.id)
        with self.assertRaises(SchedulerError):
            s.set_class('z', 'urgent')

    def test_override_survives_reregistration(self):
        s = make()
        s.start_session('a', 'again', None, '/elsewhere')
        self.assertEqual((s.sessions['a'].cls, s.sessions['a'].source), ('p1', 'override'))

    def test_finish_is_idempotent_and_fills_gaps(self):
        s = make()
        r = s.request('a', 'x', '/tmp', estimate=100 * M)
        s.decide(plenty())
        s.finish(r.id, peak=50 * M)
        ended = r.ended
        s.finish(r.id, exit=3, peak=40 * M)
        self.assertEqual((r.state, r.exit, r.peak, r.ended), ('ended', 3, 50 * M, ended))

    def test_forget_keeps_recent_finished(self):
        s = make()
        for _ in range(5):
            s.finish(s.request('a', 'x', '/tmp', estimate=1).id)
        s.forget(keep=2)
        self.assertEqual(len(s.runs), 2)
        self.assertTrue(s.idle() is False)
        for sid in list(s.sessions):
            s.end_session(sid)
        self.assertTrue(s.idle())


if __name__ == '__main__':
    unittest.main()
