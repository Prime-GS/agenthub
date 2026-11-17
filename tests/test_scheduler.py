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


class M2Test(unittest.TestCase):
    def make(self, **machine):
        data = {'machine': dict({'agent_budget': 1000 * M, 'desktop_reserve': 0, 'aging_step': '10s'}, **machine),
                'pools': {'browser': 1, 'emulator': 2},
                'kinds': [{'name': 'browser', 'match': 'playwright', 'estimate': '100M', 'pools': ['browser']}]}
        s = Scheduler(c.build(data, 8000 * M), Clock())
        for sid, cls in (('a', 'p1'), ('b', 'p2'), ('z', 'p3'), ('top', 'p0')):
            s.start_session(sid, 'test', None, '/tmp')
            s.set_class(sid, cls)
        return s

    def test_estimate_sources(self):
        s = self.make()
        self.assertEqual(s.request('a', 'x', '/tmp', estimate=5).estimate_source, 'explicit')
        learned = s.request('a', 'x', '/tmp', learned=300 * M)
        self.assertEqual((learned.estimate, learned.estimate_source), (300 * M, 'history'))
        capped = s.request('a', 'x', '/tmp', learned=5000 * M)  # an outlier must not get the run refused
        self.assertEqual((capped.estimate, capped.state), (1000 * M, 'queued'))
        kind = s.request('a', 'npx playwright test', '/tmp')
        self.assertEqual((kind.kind, kind.estimate, kind.estimate_source, kind.pools),
                         ('browser', 100 * M, 'kind browser', ['browser']))
        plain = s.request('a', 'make', '/tmp')
        self.assertEqual((plain.kind, plain.estimate_source, plain.pools), ('other', 'default', []))
        named = s.request('a', 'run-tests.sh', '/tmp', kind='browser', pools=['emulator'])
        self.assertEqual((named.kind, named.pools), ('browser', ['emulator']))
        for bad, code in (({'kind': 'nope'}, 'unknown_kind'), ({'pools': ['gpu']}, 'unknown_pool')):
            with self.assertRaises(SchedulerError) as e:
                s.request('a', 'x', '/tmp', **bad)
            self.assertEqual(e.exception.code, code)
        twice = s.request('a', 'x', '/tmp', estimate=1, pools=['browser', 'browser'])
        self.assertEqual(twice.pools, ['browser'])   # one unit, not two

    def test_pool_blocks_only_runs_that_need_it(self):
        s = self.make()
        first = s.request('a', 'playwright one', '/tmp')
        s.decide(plenty())
        second = s.request('z', 'playwright two', '/tmp')
        third = s.request('z', 'playwright three', '/tmp')
        plain = s.request('z', 'make', '/tmp', estimate=10 * M)
        self.assertEqual([r.id for r in s.decide(plenty())], [plain.id])
        self.assertEqual(second.reason, f'pool browser 1/1 held by run {first.id}')
        self.assertEqual(third.reason, f'behind run {second.id} for pool browser')
        s.finish(first.id)
        self.assertEqual([r.id for r in s.decide(plenty())], [second.id])

    def test_memory_blocker_still_blocks_everything(self):
        s = self.make()
        big = s.request('a', 'big', '/tmp', estimate=900 * M)
        s.decide(plenty())
        wait = s.request('b', 'wait', '/tmp', estimate=500 * M)
        browser = s.request('z', 'playwright', '/tmp')
        s.decide(plenty())
        self.assertEqual(browser.state, 'queued')
        self.assertIn(f'behind run {wait.id}', browser.reason)
        self.assertEqual(big.state, 'granted')

    def test_aging_lifts_to_p1_never_p0_and_keeps_the_slice(self):
        s = self.make()
        s.request('a', 'hold', '/tmp', estimate=1000 * M)
        s.decide(plenty())
        old = s.request('z', 'old', '/tmp', estimate=10 * M)
        newer = s.request('b', 'newer', '/tmp', estimate=10 * M)
        self.assertEqual([r.id for r in s.queue()], [newer.id, old.id])
        s.clock.t += 10
        self.assertEqual(s.effective_class(old), 'p2')
        s.clock.t += 100
        self.assertEqual((s.effective_class(old), s.effective_class(newer), old.cls), ('p1', 'p1', 'p3'))
        self.assertEqual([r.id for r in s.queue()], [old.id, newer.id])  # same class now: older first
        off = self.make(aging_step=0)
        r = off.request('z', 'x', '/tmp', estimate=1)
        off.clock.t += 10 ** 6
        self.assertEqual(off.effective_class(r), 'p3')
        back = self.make()
        r = back.request('z', 'x', '/tmp', estimate=1)
        back.clock.t -= 3600   # the wall clock stepped back: the run neither ages nor drops below its class
        self.assertEqual(back.effective_class(r), 'p3')
        self.assertEqual(back.queue(), [r])

    def test_pause_holds_the_queue_and_by_hand_wins(self):
        s = self.make()
        r = s.request('a', 'x', '/tmp', estimate=1)
        s.pressure = 'memory pressure — admissions paused'
        self.assertEqual(s.decide(plenty()), [])
        self.assertEqual(r.reason, 'memory pressure — admissions paused')
        s.paused_by_hand = 'demo'
        s.decide(plenty())
        self.assertEqual(r.reason, 'admissions paused by hand (demo)')
        s.paused_by_hand = s.pressure = None
        self.assertEqual(s.decide(plenty()), [r])

    def test_victim_lowest_class_newest_never_p0_or_stopping(self):
        s = self.make()
        runs = {}
        for name, sid in (('p1', 'a'), ('p3-old', 'z'), ('p3-new', 'z'), ('p0', 'top')):
            runs[name] = s.request(sid, name, '/tmp', estimate=1)
            s.decide(plenty())
            s.mark_started(runs[name].id, 'u', None)
        self.assertIs(s.victim(), runs['p3-new'])
        runs['p3-new'].stopping = 'shed'
        self.assertIs(s.victim(), runs['p3-old'])
        self.assertIs(s.victim(lambda r: r.cls != 'p3'), runs['p1'])
        for name in ('p1', 'p3-old'):
            s.finish(runs[name].id)
        self.assertIsNone(s.victim())  # only p0 is left, and p0 is not preemptible

    def test_explain_lists_checks(self):
        s = self.make()
        s.request('a', 'playwright one', '/tmp')
        s.decide(plenty())
        r = s.request('z', 'playwright two', '/tmp')
        s.decide(plenty())
        checks = {c['rule']: c['pass'] for c in s.explain(r.id, plenty())['checks']}
        self.assertEqual(checks, {'admissions': True, 'memory': True, 'pool': False})

    def test_explain_pool_is_not_free_for_a_run_behind_another_waiting_for_it(self):
        s = self.make()
        s.request('a', 'playwright one', '/tmp')
        s.decide(plenty())
        s.request('z', 'playwright two', '/tmp')
        third = s.request('z', 'playwright three', '/tmp')
        s.decide(plenty())
        s.finish(1)   # the unit is free, but run two is first in line for it
        pool = next(c for c in s.explain(third.id, plenty())['checks'] if c['rule'] == 'pool')
        self.assertEqual((pool['used'], pool['pass']), (0, False))


if __name__ == '__main__':
    unittest.main()
