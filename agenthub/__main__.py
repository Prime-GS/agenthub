import argparse
import sys

from . import __version__


def parser():
    p = argparse.ArgumentParser(prog='agenthub', description='Load hub for AI agent sessions on one Linux machine.')
    p.add_argument('--version', action='version', version=f'agenthub {__version__}')
    sub = p.add_subparsers(dest='command', required=True)

    run = sub.add_parser('run', help='run a heavy command through the hub')
    run.add_argument('--session', help='session id (default: $AGENTHUB_SESSION, else the calling shell)')
    run.add_argument('--estimate', help='expected peak memory, e.g. 2G (default: learned from past runs, '
                                        'else from kind rules)')
    run.add_argument('--kind', help='kind name from the config (default: matched from the command line)')
    run.add_argument('--pool', action='append', metavar='NAME',
                     help='take one unit of this pool while running; repeatable (default: the kind\'s pools)')
    run.add_argument('--wait', help='longest time to wait in the queue, e.g. 10m (default: no limit)')
    run.add_argument('--shell', help='run this command line with bash -c instead of argv')
    run.add_argument('--grant', type=int, metavar='RUN',
                     help='run under a grant a hook already waited for; queues as usual when it is gone')
    run.add_argument('argv', nargs=argparse.REMAINDER, help='-- command and arguments')

    st = sub.add_parser('status', help='sessions, runs and the queue')
    st.add_argument('--json', action='store_true')
    st.add_argument('--line', action='store_true',
                    help='one short line for a status line; prints nothing when the hub is not running')
    st.add_argument('--session', help='for --line: whose share to show (default: session_id of the JSON on stdin, '
                                      'as Claude Code passes it)')

    why = sub.add_parser('why', help='explain the decision about one run')
    why.add_argument('run', type=int)

    adm = sub.add_parser('admissions', help='pause or resume starting new runs (running ones are not touched)')
    adm.add_argument('action', nargs='?', choices=('pause', 'resume'), help='without it: show the state')
    adm.add_argument('--reason', default='paused by hand')

    stop = sub.add_parser('stop', help='stop a run (or take it out of the queue)')
    stop.add_argument('run', type=int)
    stop.add_argument('--reason', default='stopped by hand')

    ses = sub.add_parser('session', help='register, end or reprioritise a session')
    ses.add_argument('action', choices=('start', 'end', 'prio'))
    ses.add_argument('id')
    ses.add_argument('cls', nargs='?', help='class for prio: p0..p3')
    ses.add_argument('--agent', default='unknown')
    ses.add_argument('--pid', type=int, help='process whose exit ends the session (default: the calling shell; 0: none)')
    ses.add_argument('--cwd')
    ses.add_argument('--title', default='')

    hook = sub.add_parser('hook', help='a Claude Code hook: reads its JSON on stdin (used by the plugin)')
    hook.add_argument('event', choices=('session-start', 'session-end', 'pre-tool-use', 'post-tool-use'))

    wrap = sub.add_parser('wrap', help='run an agent without hooks (Codex CLI, a shell) as a session whose builds '
                                       'and tests go through the hub')
    wrap.add_argument('--agent', help='agent name for the session (default: the program\'s name)')
    wrap.add_argument('argv', nargs=argparse.REMAINDER, help='-- command and arguments')

    sub.add_parser('mcp', help='MCP server on stdio with a `run` tool, for agents without hooks')
    shim = sub.add_parser('shim')   # what the shims of `wrap` call; not for people
    shim.add_argument('tool')
    shim.add_argument('args', nargs=argparse.REMAINDER)

    sub.add_parser('hub', help='run the hub in the foreground (systemd starts it through the socket)')
    for name in ('install', 'uninstall'):
        i = sub.add_parser(name, help=f'{name} the systemd user units')
        i.add_argument('--runtime', action='store_true', help='units in $XDG_RUNTIME_DIR (gone after reboot; tests)')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'hub':
        from .hub import main as hub_main
        return hub_main()
    if args.command == 'hook':
        from . import claude
        return claude.main(args.event)
    if args.command in ('wrap', 'shim'):
        from . import wrap
        try:
            return getattr(wrap, f'cmd_{args.command}')(args)
        except KeyboardInterrupt:
            return 130
    if args.command == 'mcp':
        from . import mcp
        return mcp.main()
    if args.command in ('install', 'uninstall'):
        from . import install
        return getattr(install, args.command)(runtime=args.runtime)
    if args.command == 'session' and args.action == 'prio' and not args.cls:
        print('agenthub: session prio needs a class (p0..p3)', file=sys.stderr)
        return 2
    from . import client
    try:
        return getattr(client, f'cmd_{args.command}')(args)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
