"""`agenthub wrap`: an agent without hooks (Codex CLI, a terminal, a script) as a session of the hub.

The wrapper registers a session with its own pid and then becomes the agent (exec keeps the pid, so the hub ends
the session when the agent exits). Small shims for the build and test tools go first in its PATH: a shim runs a
light command directly and a heavy one — one a kind rule matches — through the hub, as `agenthub run` would.
"""
import os
import secrets
import shlex
import signal
import sys

from . import client
from . import config as configmod
from . import system

MARK = '# agenthub shim'
# the programs the built-in kinds start with; a command started by its path (`./gradlew`) passes them by
# (not interpreters such as `python3` or `node`: each of their calls would start one more interpreter)
TOOLS = ('make', 'cmake', 'ninja', 'gradle', 'mvn', 'npm', 'pnpm', 'yarn', 'bun', 'npx', 'bunx', 'tsc', 'webpack',
         'rollup', 'vite', 'next', 'nest', 'jest', 'vitest', 'mocha', 'playwright', 'cypress', 'pytest', 'uv', 'poetry',
         'rspec', 'phpunit', 'go', 'cargo')


def shim_dir(pfx):
    return os.path.join(system.state_dir(pfx), 'shims')


def write_shims(directory):
    """One small script per tool; rewritten on every wrap, so a moved checkout or interpreter takes effect."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    text = (f'#!/bin/sh\n{MARK}: heavy commands go through the hub, the rest runs directly\n'
            f'exec {shlex.join(system.self_command())} shim "${{0##*/}}" "$@"\n')
    for tool in TOOLS:
        path = os.path.join(directory, tool)
        tmp = f'{path}.{os.getpid()}'
        try:
            with open(tmp, 'w') as f:
                f.write(text)
            os.chmod(tmp, 0o755)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def become(argv, env=None, path=None):
    """exec with the signal dispositions a program expects: Python ignores SIGPIPE and SIGXFSZ, and an exec'd
    program would inherit that (`yes | head -1` would end in an error instead of quietly)."""
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(sig, signal.SIG_DFL)
    if path is not None:
        os.execv(path, argv)
    os.execvpe(argv[0], argv, env)


def is_shim(path):
    try:
        with open(path, 'rb') as f:
            return MARK.encode() in f.read(256)
    except OSError:
        return False


def real_tool(name):
    """The tool a shim stands for: the first one in PATH that is not a shim of any agenthub."""
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        path = os.path.join(directory or '.', name)
        if os.path.isfile(path) and os.access(path, os.X_OK) and not is_shim(path):
            return path
    return None


def cmd_shim(args):
    name, rest = args.tool, list(args.args)
    real = real_tool(name)
    if real is None:
        print(f'{name}: command not found', file=sys.stderr)
        return 127
    heavy = False
    if not os.environ.get('AGENTHUB_RUN'):   # inside a run already: its grant covers this one
        try:
            heavy = configmod.load().kind_for(shlex.join([name, *rest])).name != 'other'
        except Exception:   # a broken config must not stop the tool
            heavy = False
    if not heavy:
        become([real, *rest], path=real)
    # the hub classifies the command itself (its config decides the kind); `agenthub run` starts the tool by its
    # name again, and the shim it meets then sees AGENTHUB_RUN and steps aside
    from .__main__ import parser
    return client.cmd_run(parser().parse_args(['run', '--', name, *rest]))


def cmd_wrap(args):
    argv = list(args.argv or [])
    if argv[:1] == ['--']:
        argv = argv[1:]
    if not argv:
        client.say('nothing to wrap: give a command after --')
        return 2
    agent = client.agent_name(args.agent or os.path.basename(argv[0]))
    sid = f'{agent}-{secrets.token_hex(6)}'
    # the pid and name also reach `agenthub run`: when the hub was away here, it registers the session itself
    env = dict(os.environ, AGENTHUB_SESSION=sid, AGENTHUB_SESSION_PID=str(os.getpid()), AGENTHUB_AGENT=agent)
    env.pop('AGENTHUB_RUN', None)
    try:
        pfx = system.prefix()
    except ValueError as e:   # the agent still starts, only without agenthub
        client.say(f'{e}; starting {argv[0]} without agenthub')
        pfx = None
    if pfx is not None:
        try:
            write_shims(shim_dir(pfx))
            env['PATH'] = shim_dir(pfx) + os.pathsep + env.get('PATH', '')
        except OSError as e:
            client.say(f'cannot write the shims ({e}); heavy commands are not routed')
        try:
            conn = client.connect(pfx)
            try:
                reply = conn.call({'op': 'session.start', 'session': sid, 'agent': agent, 'pid': os.getpid(),
                                   'cwd': os.getcwd()})
            finally:
                conn.close()
            if not reply.get('ok'):
                client.say(f'session {sid}: {reply["error"]["message"]}')
        except (OSError, client.HubLost, ValueError) as e:
            client.say(f'hub not reachable ({e}); heavy commands run directly')
    try:
        become(argv, env)
    except OSError as e:
        client.say(f'cannot start {argv[0]}: {e.strerror or e}')
        return 127
