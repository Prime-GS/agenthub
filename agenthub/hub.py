"""agenthubd: the asyncio server that owns sessions, runs and the queue."""
import asyncio
import glob
import json
import logging
import os
import re
import socket
import sys
import time

from . import config as configmod
from . import system
from .scheduler import ACTIVE, Machine, Run, Scheduler, SchedulerError, run_from, session_from, to_dict

log = logging.getLogger('agenthub')
TICK = 2.0
GRANT_START_LIMIT = 15.0   # a granted run whose client is gone waits this long for its scope
SCOPE_GRACE = 30.0         # a started run whose scope does not exist yet is not ended before this
PROTOCOL = 1
SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}')


def send(writer, obj):
    try:
        writer.write((json.dumps(obj) + '\n').encode())
    except (ConnectionError, RuntimeError):
        pass


def error(code, message, **extra):
    return {'ok': False, 'error': {'code': code, 'message': message}, **extra}


def record(obj):
    """A session or run as the protocol shows it: `class`, like every other answer, not the field name `cls`."""
    data = to_dict(obj)
    data['class'] = data.pop('cls')
    return data


def pid_arg(value):
    return int(value) if value not in (None, 0) else None


def session_arg(msg):
    """The session id of a request; it names a file in the state directory, so only a safe shape passes."""
    sid = str(msg['session'])
    if not SESSION_ID.fullmatch(sid):
        raise SchedulerError('bad_request', f'bad session id {sid!r}: letters, digits and ._:@- only, up to 128')
    return sid


class Hub:
    def __init__(self, cfg, pfx, cgroups, state, clock=time.time, stop_unit=system.stop_unit, read_machine=None):
        self.cfg = cfg
        self.pfx = pfx
        self.cg = cgroups
        self.state = state
        self.clock = clock
        self.stop_unit = stop_unit
        self.read_machine = read_machine or self.machine_now
        self.sched = Scheduler(cfg, clock)
        self.conns = {}
        self.sent = {}
        self.watches = {}
        self.pidfds = {}
        self.last_activity = time.monotonic()
        self.inotify = None
        self.server = None
        self.done = None
        self.loop = None
        self.tick_interval = TICK

    # machine

    def machine_now(self):
        mem = system.meminfo()
        current = {r.id: system.memory_current(self.cg.run_dir(r.cls, r.id)) or 0 for r in self.sched.active()}
        return Machine(mem['MemAvailable'], system.memory_current(self.cg.base) or 0, current)

    # persistence: runtime state survives a hub restart, not a reboot

    def path(self, *parts):
        return os.path.join(self.state, *parts)

    def write_json(self, path, data):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def remove(self, path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def save_session(self, session):
        self.write_json(self.path('sessions', f'{session.id}.json'), to_dict(session))

    def save_run(self, run):
        self.write_json(self.path('runs', f'{run.id}.json'), to_dict(run))
        self.write_json(self.path('next-id.json'), {'next_id': self.sched.next_id})

    def restore(self):
        """Rebuilds sessions and running runs from the state directory and cgroupfs after a restart."""
        try:
            with open(self.path('next-id.json')) as f:
                self.sched.next_id = max(self.sched.next_id, int(json.load(f)['next_id']))
        except (OSError, ValueError, KeyError):
            pass
        for file in glob.glob(self.path('sessions', '*.json')):
            try:
                with open(file) as f:
                    session = session_from(json.load(f))
            except (OSError, ValueError, TypeError):
                self.remove(file)
                continue
            if session.pid and not self.open_pidfd(session.id, session.pid):
                self.remove(file)
                continue
            self.sched.sessions[session.id] = session
        live = {rid: (cls, d) for cls, rid, d in self.cg.scopes() if system.populated(d)}
        for file in glob.glob(self.path('runs', '*.json')):
            try:
                with open(file) as f:
                    run = run_from(json.load(f))
            except (OSError, ValueError, TypeError):
                self.remove(file)
                continue
            if run.state in ACTIVE and run.id in live:
                run.state = 'running'
                self.sched.runs[run.id] = run
            elif run.state == 'granted':
                # granted before the restart, scope not up yet: the tick adopts it or drops it
                self.sched.runs[run.id] = run
            else:
                self.remove(file)
        for rid, (cls, directory) in live.items():
            if rid not in self.sched.runs:
                self.sched.runs[rid] = Run(id=rid, session='?', agent_id=None, cmd='(unknown: no metadata)', cwd='',
                                           kind='other', cls=cls, estimate=system.memory_current(directory) or 0,
                                           requested=self.clock(), state='running', unit=system.run_unit(self.pfx, rid))
        if self.sched.runs:
            self.sched.next_id = max(self.sched.next_id, max(self.sched.runs) + 1)
        for run in self.sched.active():
            self.watch_run(run)
        log.info('restored %d sessions, %d running runs', len(self.sched.sessions), len(self.sched.active()))

    # sessions

    def open_pidfd(self, sid, pid):
        try:
            fd = os.pidfd_open(pid)
        except (OSError, TypeError, ValueError):
            return False
        self.pidfds[sid] = fd
        asyncio.get_running_loop().add_reader(fd, self.session_died, sid)
        return True

    def close_pidfd(self, sid):
        fd = self.pidfds.pop(sid, None)
        if fd is not None:
            asyncio.get_running_loop().remove_reader(fd)
            os.close(fd)

    def session_died(self, sid):
        log.info('session %s: process exited', sid)
        self.end_session(sid, 'session process exited')

    def end_session(self, sid, why):
        self.close_pidfd(sid)
        for run in self.sched.end_session(sid):
            self.reply_final(run, error('cancelled', f'run {run.id}: {why}'))
        self.remove(self.path('sessions', f'{sid}.json'))
        self.schedule()

    # runs

    def watch_run(self, run):
        if run.unit and self.inotify is not None and run.id not in self.watches.values():
            try:
                wd = self.inotify.watch(os.path.join(self.cg.run_dir(run.cls, run.id), 'cgroup.events'))
                self.watches[wd] = run.id
            except OSError:
                pass  # the scope is not there yet; the tick tries again
        self.check_run(run)

    def unwatch_run(self, rid):
        for wd, watched in list(self.watches.items()):
            if watched == rid:
                del self.watches[wd]
                if self.inotify is not None:
                    self.inotify.unwatch(wd)

    def inotify_ready(self):
        for wd in self.inotify.read():
            rid = self.watches.get(wd)
            run = self.sched.runs.get(rid) if rid is not None else None
            if run is not None:
                self.check_run(run)

    def check_run(self, run):
        """A running run whose scope is empty or gone has ended, whoever started it."""
        if run.state != 'running' or not run.unit:
            return  # without a scope the client's connection tracks the run
        directory = self.cg.run_dir(run.cls, run.id)
        if system.populated(directory):
            return
        if not os.path.isdir(directory) and self.clock() - (run.started or 0) < SCOPE_GRACE:
            return  # systemd-run has not created the scope yet
        self.end_run(run, peak=system.memory_peak(directory))

    def claim_granted(self, run):
        """A granted run whose client is gone: its scope may still come up (the client died after starting it,
        or the hub restarted in between). Adopt it once it runs; drop the grant if it never does."""
        if system.populated(self.cg.run_dir(run.cls, run.id)):
            self.sched.mark_started(run.id, system.run_unit(self.pfx, run.id), None)
            self.save_run(run)
            self.watch_run(run)
        elif self.clock() - (run.granted or 0) > GRANT_START_LIMIT:
            self.sched.finish(run.id, 'cancelled', reason='granted but never started')
            self.remove(self.path('runs', f'{run.id}.json'))
            self.schedule()

    def end_run(self, run, exit=None, peak=None):
        self.sched.finish(run.id, 'ended', exit=exit, peak=peak)
        self.unwatch_run(run.id)
        self.remove(self.path('runs', f'{run.id}.json'))
        self.schedule()

    def reply_final(self, run, message):
        writer = self.conns.pop(run.id, None)
        self.sent.pop(run.id, None)
        if writer is not None:
            send(writer, message)

    def schedule(self):
        """Admits what fits and tells every waiting client its current position and reason."""
        if not self.sched.queue():
            return
        for run in self.sched.decide(self.read_machine()):
            self.save_run(run)
            writer = self.conns.get(run.id)
            if writer is None:
                self.sched.finish(run.id, 'cancelled', reason='client gone before grant')
                self.remove(self.path('runs', f'{run.id}.json'))
                continue
            self.sent.pop(run.id, None)
            send(writer, {'ok': True, 'state': 'granted', 'run': run.id, 'unit': system.run_unit(self.pfx, run.id),
                          'slice': system.class_slice(self.pfx, run.cls), 'memory_high': run.memory_high})
        for pos, run in enumerate(self.sched.queue(), 1):
            update = (pos, run.reason)
            writer = self.conns.get(run.id)
            if writer is not None and self.sent.get(run.id) != update:
                self.sent[run.id] = update
                send(writer, {'ok': True, 'state': 'queued', 'run': run.id, 'position': pos, 'reason': run.reason})

    # protocol

    async def handle(self, reader, writer):
        sock = writer.get_extra_info('socket')
        try:
            if system.peer_uid(sock) != os.getuid():
                writer.close()
                return
        except OSError:
            writer.close()
            return
        mine = set()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                self.last_activity = time.monotonic()
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError
                except ValueError:
                    send(writer, error('bad_request', 'expected one JSON object per line'))
                    continue
                try:
                    answer = self.dispatch(msg, writer, mine)
                except SchedulerError as e:
                    answer = error(e.code, str(e))
                except configmod.ConfigError as e:
                    answer = error('bad_request', str(e))
                except (KeyError, TypeError, ValueError) as e:
                    answer = error('bad_request', f'bad field: {e}')
                except Exception as e:
                    log.exception('op %r failed', msg.get('op'))
                    answer = error('internal', f'hub error: {e}')
                if answer is not None:
                    send(writer, answer)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.connection_closed(mine)
            writer.close()

    def connection_closed(self, mine):
        """A client that leaves while queued gives up its place; a scoped run lives on without it."""
        for rid in mine:
            run = self.sched.runs.get(rid)
            self.conns.pop(rid, None)
            self.sent.pop(rid, None)
            if run is None:
                continue
            if run.state == 'queued':
                self.sched.finish(rid, 'cancelled', reason='client left the queue')
            elif run.state == 'granted':
                self.claim_granted(run)
            elif run.state == 'running' and not run.unit:
                self.end_run(run)  # no scope to watch: the client was the only witness
        self.schedule()

    def dispatch(self, msg, writer, mine):
        if msg.get('v') != PROTOCOL:
            return error('version', f'protocol version {msg.get("v")!r} is not supported, expected {PROTOCOL}')
        op = msg.get('op')
        handler = getattr(self, 'op_' + str(op).replace('.', '_'), None)
        if handler is None:
            return error('bad_request', f'unknown op {op!r}')
        return handler(msg, writer, mine)

    def op_session_start(self, msg, writer, mine):
        sid = session_arg(msg)
        pid = pid_arg(msg.get('pid'))
        session = self.sched.start_session(sid, msg.get('agent'), pid, msg.get('cwd') or '/', msg.get('title'))
        if pid and sid not in self.pidfds and not self.open_pidfd(sid, pid):
            self.sched.sessions.pop(sid, None)
            return error('bad_request', f'session process {pid} does not exist')
        self.save_session(session)
        return {'ok': True, 'session': sid, 'class': session.cls, 'source': session.source}

    def op_session_end(self, msg, writer, mine):
        self.end_session(session_arg(msg), 'session ended')
        return {'ok': True}

    def op_session_prio(self, msg, writer, mine):
        session = self.sched.set_class(session_arg(msg), msg['class'])
        self.save_session(session)
        self.schedule()
        return {'ok': True, 'class': session.cls, 'source': session.source}

    def op_run_request(self, msg, writer, mine):
        estimate = msg.get('estimate')
        wait = msg.get('wait')
        run = self.sched.request(
            session_arg(msg), str(msg['cmd']), msg.get('cwd'),
            estimate=configmod.parse_size(estimate, 'estimate') if estimate is not None else None,
            agent_id=msg.get('agent_id'),
            wait=configmod.parse_duration(wait, 'wait') if wait is not None else None)
        if run.state == 'refused':
            return error('refused', run.reason, run=run.id)
        mine.add(run.id)
        self.conns[run.id] = writer
        self.schedule()
        return None

    def op_run_started(self, msg, writer, mine):
        rid = int(msg['run'])
        # the unit name is ours to give; the client only says whether it made a scope
        run = self.sched.mark_started(rid, system.run_unit(self.pfx, rid) if msg.get('unit') else None,
                                      pid_arg(msg.get('pid')))
        mine.add(run.id)
        self.conns[run.id] = writer
        self.save_run(run)
        self.watch_run(run)
        if run.stopping and run.unit:
            self.stop(run)  # stopped while its scope was still coming up
        return None

    def op_run_ended(self, msg, writer, mine):
        run = self.sched.run(int(msg['run']))
        directory = self.cg.run_dir(run.cls, run.id)
        self.conns.pop(run.id, None)
        mine.discard(run.id)
        if run.state not in ('ended', 'cancelled'):
            self.end_run(run, exit=msg.get('exit'), peak=system.memory_peak(directory))
        else:
            self.sched.finish(run.id, exit=msg.get('exit'))
        duration = (run.ended or self.clock()) - (run.started or run.granted or run.requested)
        return {'ok': True, 'run': run.id, 'peak': run.peak, 'duration': round(duration, 1),
                'stopped': run.stopping}

    def op_run_stop(self, msg, writer, mine):
        run = self.sched.run(int(msg['run']))
        why = msg.get('reason') or 'stopped on request'
        if run.state == 'queued':
            self.sched.finish(run.id, 'cancelled', reason=why)
            self.reply_final(run, error('cancelled', f'run {run.id}: {why}'))
            self.schedule()
            return {'ok': True, 'run': run.id, 'state': 'cancelled'}
        if run.state not in ACTIVE:
            return error('bad_request', f'run {run.id} is {run.state}')
        run.stopping = why
        self.save_run(run)
        writer_of_run = self.conns.get(run.id)
        if writer_of_run is not None:
            send(writer_of_run, {'ok': True, 'state': 'stopping', 'run': run.id, 'reason': why})
        if run.unit or run.state == 'granted':
            self.stop(run)
        return {'ok': True, 'run': run.id, 'state': 'stopping'}

    def stop(self, run):
        # systemctl must not hold the event loop
        self.loop.run_in_executor(None, self.stop_unit, system.run_unit(self.pfx, run.id))

    def op_status(self, msg, writer, mine):
        m = self.read_machine()
        free, growth, headroom = self.sched.room(m)
        runs = sorted(self.sched.runs.values(), key=lambda r: r.id)
        return {'ok': True,
                'machine': {'total': self.cfg.total_ram, 'available': m.mem_available, 'reserve': self.cfg.desktop_reserve,
                            'budget': self.cfg.agent_budget, 'agent_current': m.agent_current, 'free': free,
                            'growth': growth, 'headroom': headroom, 'pressure': system.pressure('memory'),
                            'config': self.cfg.source},
                'sessions': [record(s) for s in self.sched.sessions.values()],
                'runs': [dict(record(r), current=m.run_current.get(r.id),
                              cpu_usec=system.cpu_usage_usec(self.cg.run_dir(r.cls, r.id)))
                         for r in runs if r.state in ACTIVE],
                'queue': [dict(record(r), position=i) for i, r in enumerate(self.sched.queue(), 1)],
                'recent': [record(r) for r in runs if r.state not in ACTIVE and r.state != 'queued'][-10:]}

    def op_why(self, msg, writer, mine):
        return dict(self.sched.explain(int(msg['run']), self.read_machine()), ok=True)

    # lifecycle

    async def tick(self):
        """Periodic work; a bug here ends the hub (systemd restarts it) instead of silently stopping the queue."""
        try:
            await self.ticks()
        except Exception as e:
            log.exception('tick failed')
            if not self.done.done():
                self.done.set_exception(e)

    async def ticks(self):
        while True:
            await asyncio.sleep(self.tick_interval)
            for run in self.sched.expire():
                self.reply_final(run, error('wait_expired', f'run {run.id}: {run.reason}', run=run.id))
            for run in list(self.sched.active()):
                if run.state == 'running':
                    self.watch_run(run)
                elif run.id not in self.conns:
                    self.claim_granted(run)
            self.schedule()
            self.sched.forget()
            system.notify('WATCHDOG=1')
            if self.sched.idle() and not self.conns and time.monotonic() - self.last_activity > self.cfg.idle_exit:
                log.info('idle for %.0fs, exiting', self.cfg.idle_exit)
                self.done.set_result(None)
                return

    async def serve(self, sock):
        loop = self.loop = asyncio.get_running_loop()
        self.done = loop.create_future()
        try:
            self.inotify = system.Inotify()
            loop.add_reader(self.inotify.fd, self.inotify_ready)
        except OSError as e:
            log.warning('inotify unavailable (%s); run ends are found every %.0fs', e, TICK)
            self.inotify = None
        self.restore()
        self.server = await asyncio.start_unix_server(self.handle, sock=sock)
        ticker = asyncio.create_task(self.tick())
        system.notify('READY=1')
        log.info('serving (config %s, budget %s, reserve %s)', self.cfg.source,
                 configmod.format_size(self.cfg.agent_budget), configmod.format_size(self.cfg.desktop_reserve))
        try:
            await self.done
        finally:
            ticker.cancel()
            self.server.close()
            if self.inotify is not None:
                loop.remove_reader(self.inotify.fd)
                self.inotify.close()
            system.notify('STOPPING=1')


def listening_socket(path):
    """Our own listening socket when systemd did not pass one; refuses to start next to a live hub."""
    if os.path.exists(path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
            raise SystemExit(f'agenthub hub: another hub is already listening on {path}')
        except (ConnectionRefusedError, FileNotFoundError):
            os.unlink(path)
        finally:
            probe.close()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(64)
    return sock


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s', stream=sys.stderr)
    pfx = system.prefix()
    try:
        cfg = configmod.load()
    except Exception as e:  # any config failure must stop restarts (exit 78), not crash-loop
        print(f'agenthub hub: config error: {e}', file=sys.stderr)
        return 78
    sock = system.activated_socket() or listening_socket(system.socket_path(pfx))
    hub = Hub(cfg, pfx, system.Cgroups(pfx), system.state_dir(pfx))
    asyncio.run(hub.serve(sock))
    return 0
