"""Memory pressure: PSI triggers on the pressure files, 2-second sampling of the same files, and the decisions
that follow from them (pause admissions, shed one run, resume)."""
import os
import select
import time

WINDOW_US = 2_000_000          # PSI window; unprivileged triggers need a multiple of 2 s
SOME_US = 150_000              # some ≥ 150 ms per 2 s: tasks stall on memory → pause admissions
FULL_US = 50_000               # full ≥ 50 ms per 2 s: the machine is about to thrash → shed one run
CALM_FOR = 30.0                # this long without pressure (and with headroom) → admissions resume
COOLDOWN = 10.0                # at most one shed per this many seconds
STOP_WAIT = 20.0               # no new shed while the last victim is still alive, for up to this long (TERM → KILL is 10 s)
NEAR_BUDGET = 0.9              # pressure counts only when agents use this much of their budget, or the reserve is hit
PAUSED = 'memory pressure — admissions paused'


def read_totals(path):
    """{'some': µs, 'full': µs} stalled since the start, or None when the file is not there."""
    try:
        with open(path) as f:
            lines = f.read().split('\n')
    except OSError:
        return None
    totals = {}
    for line in lines:
        kind, _, rest = line.partition(' ')
        for field in rest.split():
            if field.startswith('total='):
                totals[kind] = int(field[6:])
    return totals if 'some' in totals and 'full' in totals else None


class Triggers:
    """PSI triggers behind one epoll descriptor, which the event loop watches for reading."""

    def __init__(self):
        self.ep = select.epoll()
        self.fds = {}

    def fileno(self):
        return self.ep.fileno()

    def armed(self, path):
        return any(p == path for p, _ in self.fds.values())

    def arm(self, path):
        """True when both triggers are set; False when the kernel or the file's owner refuses (sampling still works)."""
        opened = []
        try:
            for kind, limit in (('some', SOME_US), ('full', FULL_US)):
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
                opened.append(fd)
                os.write(fd, f'{kind} {limit} {WINDOW_US}\0'.encode())
                self.ep.register(fd, select.EPOLLPRI)
                self.fds[fd] = (path, kind)
        except OSError:
            for fd in opened:
                self.drop(fd)
            return False
        return True

    def drop(self, fd):
        self.fds.pop(fd, None)
        try:
            self.ep.unregister(fd)
        except OSError:
            pass
        os.close(fd)

    def fired(self):
        """{(path, kind)} of the triggers that fired; those whose cgroup went away are dropped."""
        out = set()
        for fd, events in self.ep.poll(0):
            if fd not in self.fds:
                continue
            if events & (select.EPOLLERR | select.EPOLLHUP):
                self.drop(fd)
            elif events & select.EPOLLPRI:
                out.add(self.fds[fd])
        return out

    def close(self):
        for fd in list(self.fds):
            self.drop(fd)
        self.ep.close()


class Sampler:
    """Stall time per 2 s from two successive readings of each pressure file."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last = {}

    def rates(self, path):
        """{'some': µs per 2 s, 'full': µs per 2 s} since the previous call for this path, or None."""
        now, totals = self.clock(), read_totals(path)
        previous = self.last.get(path)
        if totals is None:
            self.last.pop(path, None)
            return None
        self.last[path] = (now, totals)
        if previous is None or now <= previous[0]:
            return None
        span = now - previous[0]
        return {kind: (totals[kind] - previous[1][kind]) * 2.0 / span for kind in ('some', 'full')}


class Governor:
    """What to do about pressure. Pure: callers pass what they measured, the clock is injectable."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.paused = None
        self.hot_at = None
        self.shed_at = None

    def assess(self, some=None, full=None, headroom=1):
        """`some`/`full`: a description of the stall that crossed its limit, or None.
        Returns why to shed one run now, or None; `self.paused` holds the pause reason."""
        now = self.clock()
        if some or full:
            self.hot_at = now
            self.paused = self.paused or PAUSED
            if full and (self.shed_at is None or now - self.shed_at >= COOLDOWN):
                self.shed_at = now
                return full
            return None
        if self.paused and now - self.hot_at >= CALM_FOR and headroom > 0:
            self.paused = None
        return None
