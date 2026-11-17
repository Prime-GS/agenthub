"""`agenthub mcp`: a Model Context Protocol server on stdio, for agents that have no hooks.

The agent starts it as an MCP server and calls its one tool, `run`, for builds, test suites and browser tests. The
command goes through `agenthub run` in a child process, so it waits in the same queue as every other session and
runs in its own scope; the answer is the exit code and the end of the output. Messages are JSON-RPC 2.0, one per
line; the server's stdout carries nothing else.
"""
import json
import os
import secrets
import select
import signal
import subprocess
import sys
import threading
import time

from . import __version__
from . import client
from . import system

PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
TAIL_LINES, TAIL_BYTES = 200, 16 << 10
LINGER = 1.0   # how long output is still read after the command ended: a child it left running may hold the pipe
RUN_TOOL = {
    'name': 'run',
    'title': 'Run a heavy command through agenthub',
    'description': ('Runs a shell command (bash -c) that needs much memory or CPU — a build, a test suite, a browser '
                    'test — after it waits for room in the queue shared by all agent sessions on this machine. '
                    'Use it instead of the shell for such commands. Returns the exit code and the last lines of '
                    'the output (stdout and stderr together).'),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'command': {'type': 'string', 'description': 'the command line, e.g. "npm run build"'},
            'cwd': {'type': 'string', 'description': 'absolute directory to run it in (default: the server\'s)'},
        },
        'required': ['command'],
    },
    # it runs whatever it is given: a client that asks before such tools asks before this one
    'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': True},
}


def reply(mid, result):
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def failure(mid, code, message):
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}


def text_result(text, error=False):
    return {'content': [{'type': 'text', 'text': text}], 'isError': error}


class Server:
    def __init__(self, inp, out):
        self.inp, self.out = inp, out
        self.lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.calls = {}           # request id → the `agenthub run` process of that call
        self.cancelled = set()    # ids of running calls the client gave up on: they get no answer
        self.sid = os.environ.get('AGENTHUB_SESSION')   # under `agenthub wrap`: the wrapper's session
        self.own = False          # whether this server registered the session (and so ends it)
        self.agent = os.environ.get('AGENTHUB_AGENT') or 'mcp'
        self.cwd = os.getcwd()

    def send(self, msg):
        data = (json.dumps(msg) + '\n').encode()
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def serve(self):
        for line in self.inp:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send(failure(None, -32700, 'parse error: one JSON-RPC message per line'))
                continue
            if not isinstance(msg, dict):
                self.send(failure(None, -32600, 'one JSON-RPC object per line (batches are not supported)'))
                continue
            try:
                self.dispatch(msg)
            except Exception as e:   # one bad message must not end the server
                if msg.get('id') is not None:
                    self.send(failure(msg.get('id'), -32603, f'{type(e).__name__}: {e}'))
        self.finish()

    def dispatch(self, msg):
        method, mid, params = msg.get('method'), msg.get('id'), msg.get('params') or {}
        if not isinstance(params, dict):
            params = {}
        if 'id' not in msg:   # a notification: never answered
            if method == 'notifications/cancelled':
                self.cancel(params.get('requestId'))
            return
        if method == 'initialize':
            self.start_session(params.get('clientInfo') or {})
            asked = params.get('protocolVersion')
            self.send(reply(mid, {
                'protocolVersion': asked if asked in PROTOCOLS else PROTOCOLS[0],
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'agenthub', 'version': __version__},
                'instructions': ('Run builds, test suites and browser tests with the `run` tool rather than the '
                                 'shell: it waits for free memory together with the other agents on this machine.'),
            }))
        elif method == 'ping':
            self.send(reply(mid, {}))
        elif method == 'tools/list':
            self.send(reply(mid, {'tools': [RUN_TOOL]}))
        elif method == 'tools/call':
            if params.get('name') != 'run':
                self.send(failure(mid, -32602, f'unknown tool {params.get("name")!r}'))
                return
            threading.Thread(target=self.call, args=(mid, params.get('arguments') or {}), daemon=True).start()
        else:
            self.send(failure(mid, -32601, f'unknown method {method!r}'))

    # the session

    def start_session(self, info):
        with self.session_lock:
            if self.sid is not None:
                return
            self.agent = client.agent_name(str(info.get('name') or 'mcp'))
            self.sid, self.own = f'mcp-{self.agent}-{secrets.token_hex(6)}', True
        try:
            conn = client.connect(system.prefix())
        except (OSError, ValueError):
            return   # `agenthub run` registers it later, with the pid and name below
        try:
            conn.call({'op': 'session.start', 'session': self.sid, 'agent': self.agent, 'pid': os.getppid(),
                       'cwd': self.cwd})
        except (client.HubLost, ValueError):
            pass
        finally:
            conn.close()

    def finish(self):
        with self.lock:
            procs = list(self.calls.values())
        for proc in procs:
            stop(proc)
        try:
            path = system.socket_path(system.prefix())
            if self.own and system.hub_running(path):
                conn = client.Conn(path)
                conn.call({'op': 'session.end', 'session': self.sid})
                conn.close()
        except (OSError, client.HubLost, ValueError):
            pass

    # the tool

    def call(self, mid, args):
        try:
            result = self.call_run(mid, args)
        except Exception as e:   # the client must get an answer, whatever went wrong here
            result = text_result(f'agenthub could not run it: {type(e).__name__}: {e}', error=True)
        if result is not None:
            self.send(reply(mid, result))

    def call_run(self, mid, args):
        """The answer to one `run` call; None for a cancelled one, which gets no answer."""
        if not isinstance(args, dict):
            return text_result('`arguments` must be an object', error=True)
        command, cwd = args.get('command'), args.get('cwd') or self.cwd
        if not isinstance(command, str) or not command.strip():
            return text_result('`command` must be a non-empty string', error=True)
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            return text_result(f'no such directory: {cwd}', error=True)
        if self.sid is None:
            self.start_session({})
        env = dict(os.environ, AGENTHUB_SESSION=self.sid, AGENTHUB_AGENT=self.agent,
                   AGENTHUB_SESSION_PID=str(os.getppid()))
        env.pop('AGENTHUB_RUN', None)
        with self.lock:
            # stdout is the protocol channel: the command gets no stdin and writes into a pipe of ours
            proc = subprocess.Popen([*system.self_command(), 'run', '--shell', command], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            self.calls[mid] = proc
        try:
            output, lines = collect(proc)
        finally:
            with self.lock:
                self.calls.pop(mid, None)
                cancelled = mid in self.cancelled
                self.cancelled.discard(mid)
        if cancelled:
            return None
        text = output.decode(errors='replace')
        shown = text.splitlines(keepends=True)[-TAIL_LINES:]
        dropped = max(lines - len(shown), 0)
        head = f'exit {client.exit_code(proc.returncode)}' + (f'; {dropped} earlier lines not shown' if dropped else '')
        return text_result(head + '\n' + ''.join(shown))

    def cancel(self, mid):
        with self.lock:
            proc = self.calls.get(mid)
            if proc is not None:
                self.cancelled.add(mid)
        if proc is not None:
            stop(proc)


def stop(proc):
    """TERM to the whole process group: `agenthub run` leaves the queue or passes it on, and the children of a
    `bash -c` line get it too."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def collect(proc):
    """(the last TAIL_BYTES of the output, the number of lines in all of it). Reads until the process ends and
    then LINGER seconds more, so a child it left behind holding the pipe cannot hold the answer."""
    fd, kept, lines, ended = proc.stdout.fileno(), b'', 0, None
    try:
        while True:
            if ended is None and proc.poll() is not None:
                ended = time.monotonic()
            left = None if ended is None else LINGER - (time.monotonic() - ended)
            if left is not None and left <= 0:
                break
            if not select.select([fd], [], [], 0.2 if left is None else min(left, 0.2))[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            lines += chunk.count(b'\n')
            kept = (kept + chunk)[-TAIL_BYTES:]
    finally:
        proc.stdout.close()
        proc.wait()
    if kept and not kept.endswith(b'\n'):
        lines += 1
    return kept, lines


def main():
    Server(sys.stdin.buffer, sys.stdout.buffer).serve()
    return 0
