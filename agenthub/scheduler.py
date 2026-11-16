"""Pure scheduling logic: sessions, runs, the queue and admission by memory. No I/O here."""
import time
from dataclasses import asdict, dataclass, field

from .config import CLASSES, format_size

ACTIVE = ('granted', 'running')
FINAL = ('ended', 'refused', 'expired', 'cancelled')
MEMORY_HIGH_FLOOR = 256 << 20


class SchedulerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass
class Session:
    id: str
    agent: str
    pid: int | None
    cwd: str
    title: str
    cls: str
    source: str
    override: str | None = None
    started: float = field(default_factory=time.time)


@dataclass
class Run:
    id: int
    session: str
    agent_id: str | None
    cmd: str
    cwd: str
    kind: str
    cls: str
    estimate: int
    requested: float
    wait: float | None = None
    state: str = 'queued'
    reason: str = ''
    granted: float | None = None
    started: float | None = None
    ended: float | None = None
    unit: str | None = None
    pid: int | None = None
    exit: int | None = None
    peak: int | None = None
    stopping: str | None = None

    @property
    def memory_high(self):
        return max(int(self.estimate * 1.5), MEMORY_HIGH_FLOOR)


@dataclass
class Machine:
    """What the kernel says right now: MemAvailable, the agent slice's memory, each run scope's memory."""
    mem_available: int
    agent_current: int
    run_current: dict = field(default_factory=dict)


class Scheduler:
    def __init__(self, config, clock=time.time, next_id=1):
        self.config = config
        self.clock = clock
        self.sessions = {}
        self.runs = {}
        self.next_id = next_id

    # sessions

    def start_session(self, sid, agent, pid, cwd, title=''):
        old = self.sessions.get(sid)
        cls, source = self.config.class_for_path(cwd)
        session = Session(sid, agent or 'unknown', pid, cwd, title or '', cls, source)
        if old and old.override:
            session.cls, session.source, session.override = old.override, 'override', old.override
        if old:
            session.started = old.started
        self.sessions[sid] = session
        return session

    def end_session(self, sid):
        """Drops the session and cancels its queued runs; running runs live on. Returns the cancelled runs."""
        self.sessions.pop(sid, None)
        cancelled = [r for r in self.queue() if r.session == sid]
        for run in cancelled:
            self.finish(run.id, 'cancelled', reason='session ended')
        return cancelled

    def set_class(self, sid, cls):
        if cls not in CLASSES:
            raise SchedulerError('bad_request', f'unknown class {cls!r}, expected one of {", ".join(CLASSES)}')
        session = self.session(sid)
        session.cls, session.source, session.override = cls, 'override', cls
        for run in self.queue():
            if run.session == sid:
                run.cls = cls
        return session

    def session(self, sid):
        session = self.sessions.get(sid)
        if session is None:
            raise SchedulerError('unknown_session', f'session {sid!r} is not registered')
        return session

    # runs

    def request(self, sid, cmd, cwd, estimate=None, agent_id=None, wait=None):
        """A queued run, or a refused one when it can never fit in the agent budget."""
        session = self.session(sid)
        kind, kind_estimate = self.config.kind_for(cmd)
        run = Run(id=self.next_id, session=sid, agent_id=agent_id, cmd=cmd, cwd=cwd or session.cwd, kind=kind,
                  cls=session.cls, estimate=estimate if estimate is not None else kind_estimate,
                  requested=self.clock(), wait=wait)
        self.next_id += 1
        self.runs[run.id] = run
        if run.estimate > self.config.agent_budget:
            self.finish(run.id, 'refused', reason=f'needs {format_size(run.estimate)}, the whole agent budget is '
                                                  f'{format_size(self.config.agent_budget)}')
        return run

    def run(self, rid):
        run = self.runs.get(rid)
        if run is None:
            raise SchedulerError('unknown_run', f'run {rid} does not exist')
        return run

    def queue(self):
        queued = [r for r in self.runs.values() if r.state == 'queued']
        return sorted(queued, key=lambda r: (CLASSES.index(r.cls), r.requested, r.id))

    def active(self):
        return [r for r in self.runs.values() if r.state in ACTIVE]

    def room(self, machine):
        """(free, growth, headroom): memory above the reserve and inside the budget, minus what admitted runs still grow."""
        cfg = self.config
        free = min(machine.mem_available - cfg.desktop_reserve, cfg.agent_budget - machine.agent_current)
        growth = sum(max(0, r.estimate - machine.run_current.get(r.id, 0)) for r in self.active())
        return free, growth, free - growth

    def decide(self, machine):
        """Grants queued runs in order while they fit; the first one that does not fit blocks the rest."""
        _, _, headroom = self.room(machine)
        granted, blocker = [], None
        for run in self.queue():
            if blocker is not None:
                run.reason = f'behind run {blocker.id} ({blocker.cls}, needs {format_size(blocker.estimate)})'
            elif run.estimate <= headroom:
                run.state, run.granted, run.reason = 'granted', self.clock(), ''
                headroom -= run.estimate
                granted.append(run)
            else:
                run.reason = f'needs {format_size(run.estimate)}, {format_size(max(headroom, 0))} above reserve'
                blocker = run
        return granted

    def expire(self):
        """Queued runs whose wait ran out; they leave the queue."""
        now, expired = self.clock(), []
        for run in self.queue():
            if run.wait is not None and now - run.requested >= run.wait:
                self.finish(run.id, 'expired', reason=f'waited {now - run.requested:.0f}s: {run.reason}')
                expired.append(run)
        return expired

    def mark_started(self, rid, unit, pid):
        run = self.run(rid)
        if run.state == 'granted':
            run.state, run.started = 'running', self.clock()
        run.unit, run.pid = unit, pid
        return run

    def finish(self, rid, state='ended', exit=None, peak=None, reason=None):
        """Moves a run to a final state; repeating it only fills in what was missing (exit code, peak)."""
        run = self.run(rid)
        if run.state not in FINAL:
            run.state, run.ended = state, self.clock()
        if exit is not None:
            run.exit = exit
        if peak is not None:
            run.peak = max(peak, run.peak or 0)
        if reason:
            run.reason = reason
        return run

    def forget(self, keep=50):
        """Keeps only the newest `keep` finished runs in memory."""
        done = sorted((r for r in self.runs.values() if r.state in FINAL), key=lambda r: r.id)
        for run in done[:-keep] if keep else done:
            del self.runs[run.id]

    def idle(self):
        return not self.sessions and not any(r.state not in FINAL for r in self.runs.values())

    def explain(self, rid, machine):
        run = self.run(rid)
        free, growth, headroom = self.room(machine)
        queue = self.queue()
        out = {'run': run.id, 'state': run.state, 'class': run.cls, 'estimate': run.estimate, 'reason': run.reason,
               'free': free, 'growth': growth, 'headroom': headroom}
        if run.state == 'queued':
            pos = queue.index(run)
            out['position'] = pos + 1
            out['ahead'] = [{'run': r.id, 'class': r.cls, 'estimate': r.estimate} for r in queue[:pos]]
            out['fits'] = run.estimate <= headroom
        return out


def to_dict(obj):
    return asdict(obj)


def session_from(data):
    return Session(**data)


def run_from(data):
    return Run(**data)
