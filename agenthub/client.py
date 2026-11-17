"""The agenthub client: run a command through the hub, show status, manage sessions. Fails open."""
import json
import os
import queue
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

from . import config as configmod
from . import system
from .config import format_size

EX_UNAVAILABLE = 69
EX_TEMPFAIL = 75
CONNECT_TIMEOUT = 1.0
ANSWER_TIMEOUT = 5.0   # a socket-activated hub that never answers (crash loop) must not hang the client
RECONNECT_FOR = 5.0
START_WAIT = 5.0


def say(text):
    print(f'agenthub: {text}', file=sys.stderr, flush=True)


class HubLost(Exception):
    pass


class Conn:
    def __init__(self, path, timeout=CONNECT_TIMEOUT):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(ANSWER_TIMEOUT)
        self.rfile = self.sock.makefile('rb')
        self.heard = False

    def patient(self):
        """After the hub has answered once, waiting in the queue may take as long as it takes."""
        self.sock.settimeout(None)

    def send(self, obj):
        try:
            self.sock.sendall((json.dumps(dict(obj, v=1)) + '\n').encode())
        except OSError as e:
            raise HubLost(str(e)) from e

    def recv(self):
        try:
            line = self.rfile.readline()
        except OSError as e:
            raise HubLost(str(e)) from e
        if not line:
            raise HubLost('connection closed')
        self.heard = True
        return json.loads(line)

    def call(self, obj):
        self.send(obj)
        return self.recv()

    def close(self):
        # shutdown first: a reader thread blocked in readline holds the file's lock until the socket wakes it
        for step in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.rfile.close, self.sock.close):
            try:
                step()
            except OSError:
                pass


def connect(pfx, timeout=CONNECT_TIMEOUT):
    return Conn(system.socket_path(pfx), timeout)


def reconnect(pfx, within=RECONNECT_FOR):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            return connect(pfx)
        except OSError:
            time.sleep(0.5)
    return None


def exit_code(returncode):
    return 128 - returncode if returncode < 0 else returncode


# sessions

def session_id(explicit):
    """(id, implicit?) — an explicit id, $AGENTHUB_SESSION, or an implicit one for the calling shell."""
    sid = explicit or os.environ.get('AGENTHUB_SESSION')
    return (sid, False) if sid else (f'pid-{os.getppid()}', True)


def register(conn, sid, implicit, cwd):
    msg = {'op': 'session.start', 'session': sid, 'agent': 'shell' if implicit else 'unknown', 'cwd': cwd,
           'pid': os.getppid() if implicit else None}
    return conn.call(msg)


# run

def run_direct(argv, cwd, why, pfx):
    """The hub is not there: run anyway, in the class slice when systemd allows, so CPU weights still apply."""
    try:
        cls, _ = configmod.load().class_for_path(cwd)
    except Exception:  # a broken config must not stop the command
        cls = 'p2'
    say(f'{why}; running without the hub (class {cls})')
    if system.can_make_scopes():
        argv = system.scope_argv(system.direct_unit(pfx), system.class_slice(pfx, cls), 'infinity', argv)
    return exit_code(subprocess.call(argv))


def wait_for_grant(conn, request):
    conn.send(request)
    shown = None
    while True:
        msg = conn.recv()
        conn.patient()
        if not msg.get('ok'):
            return msg
        if msg.get('state') == 'granted':
            return msg
        if msg.get('state') == 'queued':
            line = f'queued #{msg.get("position")} (run {msg.get("run")}): {msg.get("reason")}'
            if line != shown:
                say(line)
                shown = line


def terminal_reaches_command():
    """True when Ctrl-C from the terminal reaches the command itself (it shares our foreground process group)."""
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError):
        return False


def forward_signals(proc):
    """Passes termination signals on to the command; returns the previous handlers."""
    def forward(signum, _frame):
        if signum == signal.SIGINT and terminal_reaches_command():
            return
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass
    return {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}


def reader(conn, inbox):
    try:
        while True:
            inbox.put(conn.recv())
    except (HubLost, ValueError):
        inbox.put(None)


def execute(conn, grant, argv, pfx):
    """Runs the granted command in its scope; returns (exit code, stop reason or None)."""
    rid, unit = grant['run'], grant['unit']
    scoped = system.can_make_scopes()
    proc = subprocess.Popen(system.scope_argv(unit, grant['slice'], grant['memory_high'], argv) if scoped else argv)
    previous = forward_signals(proc)
    deadline = time.monotonic() + START_WAIT
    # report the start only once the scope exists, so the hub can watch it
    while scoped and proc.poll() is None and not system.in_unit(proc.pid, unit) and time.monotonic() < deadline:
        time.sleep(0.02)
    inbox, stopped, alive = queue.Queue(), None, True
    try:
        conn.send({'op': 'run.started', 'run': rid, 'unit': unit if scoped else None, 'pid': proc.pid})
        threading.Thread(target=reader, args=(conn, inbox), daemon=True).start()
    except HubLost:
        alive = False
    while proc.poll() is None:
        try:
            msg = inbox.get(timeout=0.5)
        except queue.Empty:
            continue
        if msg is None:
            alive = False
        elif msg.get('state') == 'stopping':
            stopped = msg.get('reason')
            say(f'run {rid} is being stopped: {stopped}')
            if not scoped:
                proc.terminate()  # no scope for systemd to stop
    for sig, handler in previous.items():
        signal.signal(sig, handler)
    code = exit_code(proc.returncode)
    report = {'op': 'run.ended', 'run': rid, 'exit': code}
    if alive:
        try:
            conn.send(report)
            while True:
                msg = inbox.get(timeout=5)
                if msg is None:
                    alive = False
                    break
                if 'peak' in msg or not msg.get('ok', True):
                    stopped = msg.get('stopped') or stopped
                    break
                if msg.get('state') == 'stopping':
                    stopped = msg.get('reason')
        except (HubLost, queue.Empty):
            alive = False
    if not alive:
        again = reconnect(pfx)
        if again is not None:
            try:
                answer = again.call(report)
                stopped = answer.get('stopped') or stopped
            except (HubLost, ValueError):
                pass
            again.close()
    return code, stopped


def cmd_run(args):
    pfx = system.prefix()
    argv = list(args.argv or [])
    if argv[:1] == ['--']:
        argv = argv[1:]
    if args.shell:
        argv = ['bash', '-c', args.shell]
    if not argv:
        say('nothing to run: give a command after -- or --shell "…"')
        return 2
    try:
        if args.estimate is not None:
            configmod.parse_size(args.estimate, '--estimate')
        if args.wait is not None:
            configmod.parse_duration(args.wait, '--wait')
    except configmod.ConfigError as e:
        say(str(e))
        return 2
    display = args.shell or shlex.join(argv)
    cwd = os.getcwd()
    sid, implicit = session_id(args.session)
    request = {'op': 'run.request', 'session': sid, 'cmd': display, 'cwd': cwd, 'estimate': args.estimate,
               'wait': args.wait, 'agent_id': os.environ.get('AGENTHUB_AGENT_ID'), 'kind': args.kind,
               'pools': args.pool}
    try:
        conn = connect(pfx)
    except OSError as e:
        return run_direct(argv, cwd, f'hub not reachable ({e.strerror or e})', pfx)
    if getattr(args, 'grant', None) is not None:
        try:
            claimed = conn.call({'op': 'run.claim', 'run': args.grant, 'session': sid})
        except (HubLost, ValueError):
            claimed = {'ok': False, 'error': {'message': 'the hub did not answer'}}
        if claimed.get('state') == 'granted':
            return finish_run(conn, claimed, argv, pfx)
        say(f'grant {args.grant} is gone ({claimed.get("error", {}).get("message")}); queueing again')
    registered = reconnects = 0
    while True:
        try:
            answer = wait_for_grant(conn, request)
        except (HubLost, ValueError):
            conn.close()
            if not conn.heard:
                return run_direct(argv, cwd, 'the hub accepted the connection but did not answer', pfx)
            reconnects += 1
            conn = reconnect(pfx) if reconnects <= 3 else None
            if conn is None:
                return run_direct(argv, cwd, 'the hub went away while this run was queued', pfx)
            say('hub restarted; queueing again')
            continue
        code = (answer.get('error') or {}).get('code')
        if code == 'unknown_session' and registered == 0:
            registered += 1
            reply = register(conn, sid, implicit, cwd)
            if not reply.get('ok'):
                conn.close()
                return run_direct(argv, cwd, f'cannot register session: {reply["error"]["message"]}', pfx)
            continue
        break
    if not answer.get('ok'):
        err = answer['error']
        conn.close()
        if err['code'] == 'refused':
            say(f'refused: {err["message"]}')
            return EX_UNAVAILABLE
        if err['code'] in ('wait_expired', 'cancelled'):
            say(f'{err["code"]}: {err["message"]}')
            return EX_TEMPFAIL
        if err['code'] in ('unknown_kind', 'unknown_pool'):
            say(f'{err["message"]}')   # a wrong --kind or --pool is the caller's mistake, not a broken hub
            return 2
        return run_direct(argv, cwd, f'hub answered {err["code"]}: {err["message"]}', pfx)
    return finish_run(conn, answer, argv, pfx)


def finish_run(conn, grant, argv, pfx):
    code, stopped = execute(conn, grant, argv, pfx)
    conn.close()
    if stopped:
        say(f'run {grant["run"]} was stopped by the hub: {stopped}')
        return EX_TEMPFAIL
    return code


# status and control

def request(msg):
    try:
        conn = connect(system.prefix())
    except OSError as e:
        say(f'hub not reachable: {e.strerror or e}')
        return None
    try:
        return conn.call(msg)
    except (HubLost, ValueError) as e:
        say(f'hub connection failed: {e}')
        return None
    finally:
        conn.close()


def age(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60}m{seconds % 60:02d}s'


def short(text, width):
    return text if len(text) <= width else text[:width - 1] + '…'


def print_status(st):
    m = st['machine']
    psi = m.get('pressure', {})

    def avg(source, kind):
        return f'{psi.get(source, {}).get(kind, {}).get("avg10", 0):.0f}%'
    print(f'machine  {format_size(m["total"])}  available {format_size(m["available"])}  '
          f'reserve {format_size(m["reserve"])}  budget {format_size(m["budget"])}  '
          f'agents {format_size(m["agent_current"])}  headroom {format_size(m["headroom"])}')
    print(f'pressure memory some {avg("memory", "some")} full {avg("memory", "full")}  '
          f'agents some {avg("agents", "some")} full {avg("agents", "full")}  cpu some {avg("cpu", "some")}  '
          f'admissions {m.get("admissions", {}).get("reason") or "open"}')
    if m.get('pools'):
        print('pools    ' + '  '.join(f'{name} {len(p["held_by"])}/{p["size"]}' for name, p in m['pools'].items()))
    now = time.time()
    print(f'{"SESSION":<16} {"AGENT":<12} {"CLASS":<5} {"WHY":<26} CWD')
    for s in st['sessions']:
        print(f'{short(s["id"], 16):<16} {short(s["agent"], 12):<12} {s["class"]:<5} {short(s["source"], 26):<26} {s["cwd"]}')
    print(f'{"RUN":<5} {"SESSION":<16} {"CLASS":<5} {"KIND":<8} {"MEM now/est":<20} {"CPU":>6} {"AGE":>7}  COMMAND')
    for r in st['runs']:
        elapsed = now - (r['started'] or r['granted'] or now)
        cpu = f'{r["cpu_usec"] / 1e6 / elapsed * 100:.0f}%' if r.get('cpu_usec') and elapsed > 0 else '-'
        mem = f'{format_size(r.get("current"))}/{format_size(r["estimate"])}'
        flag = f'  [stopping: {r["stopping"]}]' if r.get('stopping') else ''
        print(f'{r["id"]:<5} {short(r["session"], 16):<16} {r["class"]:<5} {r["kind"]:<8} {mem:<20} {cpu:>6} '
              f'{age(elapsed):>7}  {short(r["cmd"], 60)}{flag}')
    for q in st['queue']:
        cls = q['class'] if q.get('effective_class', q['class']) == q['class'] else f'{q["class"]}→{q["effective_class"]}'
        print(f'QUEUE #{q["position"]} run {q["id"]}  {cls}  {q["kind"]}  '
              f'est {format_size(q["estimate"])} ({q.get("estimate_source", "?")})  '
              f'waiting {age(now - q["requested"])}: {q["reason"]}  ·  {short(q["cmd"], 50)}')


def status_line(st, sid):
    """`hub ▸ room 9.8 GiB · you p1 · 2 running · 1 queued · queue 3 · paused`."""
    m = st['machine']
    parts = [f'hub ▸ room {format_size(max(m["headroom"], 0))}']
    session = next((s for s in st['sessions'] if s['id'] == sid), None)
    if session is not None:
        parts.append(f'you {session["class"]}')
        running = sum(1 for r in st['runs'] if r['session'] == sid)
        queued = sum(1 for q in st['queue'] if q['session'] == sid)
        parts += [f'{running} running'] if running else []
        parts += [f'{queued} queued'] if queued else []
    if st['queue']:
        parts.append(f'queue {len(st["queue"])}')
    if m.get('admissions', {}).get('state') == 'paused':
        parts.append('paused')
    return ' · '.join(parts)


def piped_input(limit=1.0):
    """What the caller pipes in, read until end of file or for `limit` seconds: a pipe left open must not hang."""
    deadline, chunks = time.monotonic() + limit, []
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            return b''
        while (left := deadline - time.monotonic()) > 0 and select.select([fd], [], [], left)[0]:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (OSError, ValueError):
        pass
    return b''.join(chunks)


def cmd_status_line(args):
    """For a status line that refreshes often: never starts the hub, never waits long, never fails loudly."""
    sid = args.session
    if sid is None:
        try:
            sid = json.loads(piped_input()).get('session_id')
        except (ValueError, AttributeError):
            sid = None
    path = system.socket_path(system.prefix())
    if not system.hub_running(path):
        return 0
    try:
        conn = Conn(path)
        conn.sock.settimeout(1.0)
        st = conn.call({'op': 'status'})
        conn.close()
    except (OSError, HubLost, ValueError):
        return 0
    if st.get('ok'):
        print(status_line(st, sid))
    return 0


def cmd_status(args):
    if args.line:
        return cmd_status_line(args)
    st = request({'op': 'status'})
    if st is None:
        return 1
    if args.json:
        print(json.dumps(st, indent=2))
    elif not st.get('ok'):
        say(st['error']['message'])
        return 1
    else:
        print_status(st)
    return 0


def print_answer(answer):
    if answer is None:
        return 1
    if not answer.get('ok'):
        say(f'{answer["error"]["code"]}: {answer["error"]["message"]}')
        return 1
    print(json.dumps(answer, indent=2))
    return 0


def cmd_why(args):
    return print_answer(request({'op': 'why', 'run': args.run}))


def cmd_admissions(args):
    msg = {'op': 'admissions'}
    if args.action:
        msg.update(state='paused' if args.action == 'pause' else 'running', reason=args.reason)
    answer = request(msg)
    if answer is None or not answer.get('ok'):
        return print_answer(answer)
    print(f'admissions {answer["state"]}' + (f': {answer["reason"]}' if answer.get('reason') else ''))
    if answer.get('by_hand') and answer.get('pressure'):
        print(f'also: {answer["pressure"]} (lifts itself when memory is calm)')
    return 0


def cmd_stop(args):
    return print_answer(request({'op': 'run.stop', 'run': args.run, 'reason': args.reason}))


def cmd_session(args):
    if args.action == 'start':
        pid = os.getppid() if args.pid is None else args.pid
        msg = {'op': 'session.start', 'session': args.id, 'agent': args.agent, 'pid': pid,
               'cwd': os.path.abspath(args.cwd or os.getcwd()), 'title': args.title}
    elif args.action == 'end':
        msg = {'op': 'session.end', 'session': args.id}
    else:
        msg = {'op': 'session.prio', 'session': args.id, 'class': args.cls}
    return print_answer(request(msg))
