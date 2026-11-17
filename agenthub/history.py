"""Learned estimates: the peak memory of past runs, kept per fingerprint in a small SQLite file.

Only the fingerprint (program, subcommand, project root) is stored, never the full command line, so arguments
that carry secrets do not end up on disk.
"""
import logging
import os
import re
import shlex
import sqlite3
import time

log = logging.getLogger('agenthub')
KEEP_PER_FINGERPRINT = 50
SAMPLES = 20
SHED_FACTOR = 1.5          # a run stopped under pressure needed more than it reached
WORD = re.compile(r'[A-Za-z][\w:-]*')   # a subcommand; not an option value, a path or a file name
WRAPPERS = {'npx', 'nohup', 'stdbuf', 'command', 'exec', 'time'}
# wrappers with arguments of their own: the options that take a value, and how many positional arguments follow
WRAPPERS_WITH_ARGS = {'env': ({'-u', '--unset', '-C', '--chdir'}, 0),
                      'timeout': ({'-s', '-k', '--signal', '--kill-after'}, 1),
                      'taskset': ({'-c', '--cpu-list', '-p'}, 0),
                      'nice': ({'-n', '--adjustment'}, 0),
                      'ionice': ({'-c', '-n', '-p', '--class', '--classdata'}, 0)}
SEPARATORS = {';', '&&', '||', '&'}
PIPES = {'|', '|&'}
REDIRECT = re.compile(r'\d*[<>]+&?|&>>?')   # `>`, `2>`, `>>`, `&>`, `<`: the next word is a file, not an argument


def strip_wrappers(words):
    """Drops `VAR=value` prefixes and wrappers such as `timeout 6`, `taskset -c 0-3`, `nice -n 19`, `npx`."""
    while words:
        head = os.path.basename(words[0])
        if '=' in words[0] and not words[0].startswith('=') or head in WRAPPERS:
            words = words[1:]
        elif head in WRAPPERS_WITH_ARGS:
            valued, positional = WRAPPERS_WITH_ARGS[head]
            words = words[1:]
            while words and words[0].startswith('-'):
                words = words[2:] if words[0] in valued else words[1:]
            if head == 'taskset' and words and not words[0].startswith('-') and re.fullmatch(r'0x[0-9a-fA-F]+|\d+',
                                                                                            words[0]):
                words = words[1:]  # a CPU mask
            words = words[positional:]
        else:
            return words
    return words


def last_command(cmd):
    """The words of the command that matters in a shell line: the last one after `;`, `&&`, `||` (a leading
    `cd` is not it), and the first stage of a pipeline. Quotes are honoured, so `-c 'a; b'` stays one word."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        tokens = cmd.split()
    commands, skip = [[]], False
    for token in tokens:
        if skip:
            skip = False
        elif token in SEPARATORS:
            commands.append([])
        elif REDIRECT.fullmatch(token):
            skip = True
        elif token not in ('(', ')'):
            commands[-1].append(token)
    commands = [c for c in commands if c and c[0] != 'cd'] or [[]]
    words = commands[-1]
    cut = next((i for i, w in enumerate(words) if w in PIPES), len(words))
    return words[:cut]


def project_root(cwd):
    """The nearest directory upwards that holds `.git`, else `cwd` itself."""
    path = os.path.realpath(cwd or '/')
    probe = path
    while True:
        if os.path.exists(os.path.join(probe, '.git')):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return path
        probe = parent


def fingerprint(cmd, cwd):
    """`program subcommand @ project root`: what makes two runs comparable for memory."""
    words = strip_wrappers(last_command(cmd))
    if words and os.path.basename(words[0]) in ('sh', 'bash', 'zsh') and len(words) >= 3 and words[1] == '-c':
        return fingerprint(words[2], cwd)
    program = os.path.basename(words[0]) if words else '?'
    rest = [w for w in words[1:] if WORD.fullmatch(w)]
    sub = ' '.join(rest[:2]) if rest[:1] in (['run'], ['exec']) else ' '.join(rest[:1])
    return f'{program} {sub}'.strip() + f' @ {project_root(cwd)}'


def p90(values):
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * 9 // 10) - 1)]


class History:
    """Never fails the hub: a broken or locked file only costs the learned estimates."""

    def __init__(self, path):
        self.path = path
        self.db = None
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)   # a file left with wider permissions is narrowed too
            finally:
                os.close(fd)
            # the hub uses it from its event loop only; tests create it in another thread
            self.db = sqlite3.connect(path, timeout=1, check_same_thread=False)
            self.db.execute('CREATE TABLE IF NOT EXISTS runs (fp TEXT NOT NULL, ts REAL NOT NULL, cls TEXT, kind TEXT, '
                            'estimate INTEGER, peak INTEGER NOT NULL, cpu_seconds REAL, duration REAL, shed INTEGER NOT NULL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS runs_fp_ts ON runs (fp, ts)')
            self.db.commit()
        except (OSError, sqlite3.Error) as e:
            log.warning('history %s unavailable (%s); estimates come from rules only', path, e)
            self.close()

    def record(self, fp, run, shed):
        if self.db is None or not fp or not run.peak:
            return
        duration = (run.ended or time.time()) - (run.started or run.granted or run.requested)
        try:
            with self.db:
                self.db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                (fp, time.time(), run.cls, run.kind, run.estimate, run.peak,
                                 run.cpu_usec / 1e6 if run.cpu_usec else None, duration, int(shed)))
                self.db.execute('DELETE FROM runs WHERE rowid IN (SELECT rowid FROM runs WHERE fp = ? '
                                'ORDER BY ts DESC LIMIT -1 OFFSET ?)', (fp, KEEP_PER_FINGERPRINT))
        except sqlite3.Error as e:
            log.warning('history: cannot record run %s: %s', run.id, e)

    def estimate(self, fp):
        """The 90th percentile of the last runs' peaks, or None without history."""
        if self.db is None or not fp:
            return None
        try:
            rows = self.db.execute('SELECT peak, shed FROM runs WHERE fp = ? ORDER BY ts DESC LIMIT ?',
                                   (fp, SAMPLES)).fetchall()
        except sqlite3.Error as e:
            log.warning('history: cannot read: %s', e)
            return None
        return p90([int(peak * SHED_FACTOR) if shed else peak for peak, shed in rows]) if rows else None

    def close(self):
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error as e:
                log.warning('history: close failed: %s', e)
            self.db = None


def default_path(pfx):
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(base, pfx, 'history.sqlite3')
