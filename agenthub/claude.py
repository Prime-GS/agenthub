"""Claude Code hooks, used by the plugin in `claude-code/`.

Every hook fails open: a missing hub, a broken config, an unreadable settings file or any error of our own ends in
exit 0 with nothing printed, so Claude Code goes on as if the plugin were not there.
"""
import json
import os
import re
import shlex
import shutil
import sys
import time

from . import client
from . import config as configmod
from . import system

AGENT = 'claude-code'
HOLD = 120     # a grant waits this long for the Bash call: a permission prompt may come in between
WAIT = 540     # the hook's own limit; Claude Code stops a hook after its 600 s timeout and runs the call unwrapped
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELLS = {'sh', 'dash', 'bash', 'zsh'}
THROUGH_HUB = re.compile(r'(?:^|[\s;&|(/])agenthub\s+run\b|-m\s+agenthub\s+run\b')
COMPLEX = ('$(', '`', '<(', '>(', '\n', '\r', '<<')   # the simple commands of such a line cannot be told apart
REFUSED = set('()\\#')   # unquoted: subshells, escapes, comments — beyond what `split_line` follows
SAFE_REDIRECT = re.compile(r'\d?>&\d|(?:\d|&)?(?:>>?|<)/dev/null')
BARE_REDIRECT = re.compile(r'(?:\d|&)?(?:>>?|<)')
CLI_RULES = {'--allowedTools': 'allow', '--allowed-tools': 'allow', '--disallowedTools': 'deny',
             '--disallowed-tools': 'deny'}


def log(text):
    print(f'agenthub hook: {text}', file=sys.stderr)


# who is Claude Code

def ancestors(pid):
    """[(pid, comm)] from `pid` up to init."""
    out = []
    while pid > 1:
        try:
            with open(f'/proc/{pid}/stat') as f:
                head, tail = f.read().rsplit(')', 1)
        except OSError:
            break
        out.append((pid, head.split('(', 1)[1]))
        pid = int(tail.split()[1])
    return out


def claude_pid():
    """The Claude Code process: hooks run as `sh -c` children of it. `CLAUDE_PID` is used when it really is an
    ancestor (it is not documented), then the nearest `claude` process, then the parent of the hook's shell."""
    chain = ancestors(os.getppid())
    pids = [pid for pid, _ in chain]
    env = os.environ.get('CLAUDE_PID', '')
    if env.isdigit() and int(env) in pids:
        return int(env)
    for pid, comm in chain:
        if comm == 'claude':
            return pid
    if len(chain) > 1 and chain[0][1] in SHELLS:
        return chain[1][0]
    return os.getppid()


def register(conn, data):
    return conn.call({'op': 'session.start', 'session': data['session_id'], 'agent': AGENT, 'pid': claude_pid(),
                      'cwd': data.get('cwd') or os.getcwd()})


# permission rules: the user's rules are written for the command the agent wrote, not for the wrapped one

def cli_options(pid):
    """The permission options Claude Code was started with, from its command line: {'allow': [specs],
    'deny': [specs], 'settings': [file or JSON], 'sources': 'user,project,local' or None}."""
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            argv = f.read().decode(errors='replace').split('\0')
    except OSError:
        argv = []
    out = {'allow': [], 'deny': [], 'settings': [], 'sources': None}
    i = 0
    while i < len(argv):
        name, eq, value = argv[i].partition('=')
        i += 1
        if name in CLI_RULES:   # variadic: values up to the next option
            values = [value] if eq else []
            while not eq and i < len(argv) and argv[i] and not argv[i].startswith('-'):
                values.append(argv[i])
                i += 1
            for text in values:
                out[CLI_RULES[name]] += re.findall(r'[^,\s()]+(?:\([^)]*\))?', text)
        elif name in ('--settings', '--setting-sources') and (eq or i < len(argv)):
            if not eq:
                value, i = argv[i], i + 1
            if name == '--settings':
                out['settings'].append(value)
            else:
                out['sources'] = value
    return out


ALL = ('allow', 'ask', 'deny')


def settings_files(cwd, sources=None):
    """[(settings file, rule lists taken from it)]; `sources` is Claude Code's `--setting-sources`, if given.
    `settings.local.json` next to the user settings is read for deny and ask only: it may not apply here."""
    home = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')
    project = os.environ.get('CLAUDE_PROJECT_DIR') or cwd or os.getcwd()
    wanted = set(re.split(r'[,\s]+', sources)) if sources is not None else {'user', 'project', 'local'}
    files = [('/etc/claude-code/managed-settings.json', ALL), (os.path.join(home, 'settings.local.json'), ALL[1:])]
    if 'user' in wanted:
        files.append((os.path.join(home, 'settings.json'), ALL))
    if 'project' in wanted:
        files.append((os.path.join(project, '.claude', 'settings.json'), ALL))
    if 'local' in wanted:
        files.append((os.path.join(project, '.claude', 'settings.local.json'), ALL))
    return files


def add_rules(rules, kind, specs):
    for rule in specs:
        if not isinstance(rule, str):
            continue
        if rule.strip() == 'Bash':
            rules[kind].append('*')
        elif rule.startswith('Bash(') and rule.endswith(')'):
            rules[kind].append(rule[5:-1].strip())


def bash_rules(files, cli=None, cwd=None):
    """{'allow': [...], 'ask': [...], 'deny': [...]}: the parts of `Bash(...)` rules, `*` for a bare `Bash`, from
    the settings files and from Claude Code's own command line (`cli_options`)."""
    rules = {'allow': [], 'ask': [], 'deny': []}
    cli = cli or {'allow': [], 'deny': [], 'settings': []}
    sources = [(f, ALL) if isinstance(f, str) else f for f in files]
    for value in cli['settings']:
        sources.append((value if value.lstrip().startswith('{') else os.path.join(cwd or '', value), ALL))
    for source, kinds in sources:
        try:
            if source.lstrip().startswith('{'):
                perms = json.loads(source).get('permissions') or {}
            else:
                with open(source) as f:
                    perms = json.load(f).get('permissions') or {}
        except FileNotFoundError:
            continue
        except (OSError, ValueError, AttributeError) as e:
            raise ValueError(f'{source[:200]}: {e}') from e
        for kind in kinds:
            add_rules(rules, kind, perms.get(kind) or [])
    add_rules(rules, 'allow', cli['allow'])
    add_rules(rules, 'deny', cli['deny'])
    return rules


def rule_pattern(rule):
    """`npm run test:*` (prefix), `git * main` (wildcards) or an exact command."""
    if rule.endswith(':*'):
        return re.compile(re.escape(rule[:-2].strip()) + r'(?:\s.*)?', re.S)
    return re.compile('.*'.join(re.escape(part) for part in rule.split('*')), re.S)


def flatten(text):
    """Quotes and backslashes out, whitespace runs to one space: `'git'  push` and `\\git push` read `git push`."""
    return re.sub(r'\s+', ' ', re.sub(r'[\'"\\]', '', text)).strip()


def mentions(command, rule):
    """Broad on purpose (for deny and ask): the fixed start of the rule appears anywhere at a word start, in the
    line as written or with quotes, backslashes and repeated spaces taken out."""
    fixed = re.split(r':\*|\*', rule, maxsplit=1)[0]
    return any(not part or re.search(r'(?:^|[\s;&|(`$"\'])' + re.escape(part), text)
               for part, text in ((fixed.strip(), command), (flatten(fixed), flatten(command))))


def split_line(command):
    """The simple commands of a shell line as lists of (word, redirect) — `redirect` when the word has an unquoted
    `<`, `>` or `&` — split at `&&`, `||`, `;`, `|`, `|&` and `&`. None when the line uses more of the shell
    grammar than that: substitutions, heredocs, subshells, escapes, comments, line breaks, an open quote."""
    if any(mark in command for mark in COMPLEX):
        return None
    pieces, words, word, special, quote, i = [], [], None, False, None, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif quote == '"' and c == '\\':
                return None
            word += c
        elif c in '\'"':
            if c == "'" and word and word.endswith('$'):
                return None   # `$'…'` takes backslash escapes, `\'` among them
            quote, word = c, (word or '') + c
        elif c in ' \t':
            if word is not None:
                words.append((word, special))
                word, special = None, False
        elif c in '<>' or (c == '&' and ((special and word[-1] in '<>') or command[i + 1:i + 2] == '>')):
            word, special = (word or '') + c, True
        elif c in ';&|':
            if word is not None:
                words.append((word, special))
                word, special = None, False
            if not words:
                return None   # an operator with no command before it
            pieces.append(words)
            words = []
            i += 2 if command[i:i + 2] in ('&&', '||', '|&') else 1
            continue
        elif c in REFUSED:
            return None
        else:
            word = (word or '') + c
        i += 1
    if quote:
        return None
    if word is not None:
        words.append((word, special))
    if words:
        pieces.append(words)
    return pieces or None


def plain(words):
    """One simple command as text without the redirections that cannot hurt (to `/dev/null`, between streams);
    None when it has another one."""
    out, i = [], 0
    while i < len(words):
        word, special = words[i]
        if not special:
            out.append(word)
        elif BARE_REDIRECT.fullmatch(word) and words[i + 1:i + 2] == [('/dev/null', False)]:
            i += 1
        elif not SAFE_REDIRECT.fullmatch(word):
            return None
        i += 1
    return ' '.join(out)


def judge(command, rules):
    """'deny' or 'ask' when a rule of that list could be about this command — or when there is such a rule and
    the line cannot be split into its commands; 'allow' only when every command of the line matches an allow
    rule; None otherwise (Claude Code decides as usual)."""
    pieces = split_line(command)
    for verdict in ('deny', 'ask'):
        if any(mentions(command, rule) for rule in rules[verdict]) or (pieces is None and rules[verdict]):
            return verdict
    if pieces is None:
        return None
    texts = [plain(words) for words in pieces]
    if all(text is not None and any(rule_pattern(r).fullmatch(text) for r in rules['allow']) for text in texts):
        return 'allow'
    return None


# the hooks

def launcher():
    """How the Bash call reaches this very agenthub: the command on PATH when it is the wrapper `agenthub install`
    wrote for this package, else this interpreter and package (an older agenthub would not know `--grant`)."""
    direct = f'PYTHONPATH={shlex.quote(ROOT)} {shlex.quote(sys.executable)} -P -m agenthub'
    found = shutil.which('agenthub')
    try:
        with open(found or '', 'rb') as f:
            ours = f'PYTHONPATH={shlex.quote(ROOT)} '.encode() in f.read(4096)
    except OSError:
        ours = False
    return 'agenthub' if ours else direct


def wrapped(command, rid, sid):
    return f'{launcher()} run --grant {rid} --session {shlex.quote(sid)} --shell {shlex.quote(command)}'


def wait_for_grant(conn, msg, data):
    """The hub's final answer to `msg`: a grant or an error; registers the session once when the hub asks.
    A hub that stays silent past WAIT ends in HubLost before Claude Code's own 600 s timeout of the hook."""
    deadline = time.monotonic() + WAIT + 20
    conn.send(msg)
    registered = False
    while True:
        answer = conn.recv()
        conn.sock.settimeout(max(1.0, deadline - time.monotonic()))
        if answer.get('state') == 'queued':
            continue
        code = (answer.get('error') or {}).get('code')
        if code == 'unknown_session' and not registered:
            registered = True
            if not register(conn, data).get('ok'):
                return answer
            conn.send(msg)
            continue
        return answer


def pre_tool_use(data):
    if data.get('tool_name') != 'Bash' or data.get('permission_mode') == 'plan':
        return None   # in plan mode the call will not run: a grant would only hold memory
    tool_input = data.get('tool_input') or {}
    command = tool_input.get('command')
    if not isinstance(command, str) or not command.strip() or THROUGH_HUB.search(command):
        return None
    if configmod.load().kind_for(command).name == 'other':
        return None
    cwd = data.get('cwd') or os.getcwd()
    try:
        cli = cli_options(claude_pid())
        rules = bash_rules(settings_files(cwd, cli['sources']), cli, cwd)
        verdict = judge(command, rules)
    except ValueError as e:
        log(f'permission rules unreadable ({e}); leaving the call as written')
        return None
    if verdict == 'deny':
        return None   # Claude Code's own deny rule must see the command as written
    sid = data['session_id']
    try:
        conn = client.connect(system.prefix())
    except OSError:
        return None
    try:
        answer = wait_for_grant(conn, {'op': 'run.request', 'session': sid, 'cmd': command, 'cwd': cwd,
                                       'agent_id': data.get('agent_id'), 'hold': HOLD, 'wait': WAIT,
                                       'notify': True}, data)
    except (client.HubLost, ValueError):
        return None
    finally:
        conn.close()
    out = {'hookEventName': 'PreToolUse'}
    if answer.get('state') == 'granted':
        new = wrapped(command, answer['run'], sid)
        if (verdict is None and data.get('permission_mode') != 'bypassPermissions'
                and any(r != '*' and rule_pattern(r).fullmatch(new) for r in rules['allow'])):
            verdict = 'ask'   # a rule allows `agenthub run …` as such: it must not allow what the user would be asked
        out['updatedInput'] = dict(tool_input, command=new)
        if verdict is not None:
            out['permissionDecision'] = verdict
            out['permissionDecisionReason'] = ('agenthub runs it under its grant; your permission rules match the '
                                               'command as written')
        return {'hookSpecificOutput': out}
    code = (answer.get('error') or {}).get('code')
    if code in ('wait_expired', 'refused'):
        message = answer['error']['message']
        why = message.split(': ', 1)[1] if message.startswith('run ') and ': ' in message else message
        out['permissionDecision'] = 'deny'
        out['permissionDecisionReason'] = (f'agenthub: {why}; retry later or run it in the background'
                                           if code == 'wait_expired' else f'agenthub: refused — {why}')
        return {'hookSpecificOutput': out}
    return None   # cancelled, a hub error, anything else: run as written


def session_start(data):
    env_file = os.environ.get('CLAUDE_ENV_FILE')
    if env_file:
        with open(env_file, 'a') as f:
            f.write(f'export AGENTHUB_SESSION={shlex.quote(data["session_id"])}\n')
    try:
        conn = client.connect(system.prefix())
    except OSError:
        return None
    try:
        register(conn, data)
    except (client.HubLost, ValueError):
        pass
    finally:
        conn.close()
    return None


def session_end(data):
    path = system.socket_path(system.prefix())
    if not system.hub_running(path):
        return None   # nothing to end, and connecting would start the hub
    try:
        conn = client.Conn(path, timeout=0.5)
        conn.sock.settimeout(0.8)   # Claude Code gives all SessionEnd hooks 1.5 s together
        conn.call({'op': 'session.end', 'session': data['session_id']})
        conn.close()
    except (OSError, client.HubLost, ValueError):
        pass
    return None


def post_tool_use(data):
    """News the hub left for this session (room appeared after a wait ran out); reads a file, never the socket."""
    path = os.path.join(system.state_dir(system.prefix()), 'news', data['session_id'])
    taken = f'{path}.{os.getpid()}'
    try:
        os.rename(path, taken)
    except OSError:
        return None
    try:
        with open(taken) as f:
            lines = [line.strip() for line in f if line.strip()]
    finally:
        os.unlink(taken)
    if not lines:
        return None
    return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': '\n'.join(lines)}}


HOOKS = {'session-start': session_start, 'session-end': session_end, 'pre-tool-use': pre_tool_use,
         'post-tool-use': post_tool_use}


def main(event):
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or not isinstance(data.get('session_id'), str):
            return 0
        out = HOOKS[event](data)
    except Exception as e:   # fail open: the plugin must never stop Claude Code
        log(f'{event} failed: {type(e).__name__}: {e}')
        return 0
    if out is not None:
        print(json.dumps(out))
    return 0
