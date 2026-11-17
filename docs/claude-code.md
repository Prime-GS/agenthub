# Claude Code integration

Claude Code is the first agent agenthub integrates with. The integration is a plugin made of hooks, in
[`claude-code/`](../claude-code), plus a status line command; it relies only on documented hook behaviour
([hooks reference](https://code.claude.com/docs/en/hooks)), checked against Claude Code 2.1.

## Using it

```sh
python3 -m agenthub install                          # the hub (once per machine)
claude plugin marketplace add /path/to/agenthub      # the checkout's marketplace (or: Prime-GS/agenthub)
claude plugin install agenthub@agenthub              # every new session, in any directory
claude --plugin-dir /path/to/agenthub/claude-code    # or: one session with the plugin
```

The hooks call `claude-code/hooks/hook.sh`, which runs the agenthub package of the same checkout; an installed
plugin is a copy in Claude Code's plugin cache, so there it runs the `agenthub` command — from `PATH`, or the one
`agenthub install` wrote to `~/.local/bin` — which runs the package of the checkout it was installed from. With
neither it does nothing. So the checkout stays where it was installed from, a change of the Python package
applies at once, and a change of `claude-code/` reaches new sessions after `claude plugin marketplace update
agenthub` and `claude plugin update agenthub@agenthub` (with a new `version` in `plugin.json`).

The plugin's hooks run next to any hooks of the user's own settings; Claude Code runs them all. A rewritten
command calls the same package: `agenthub` when that command is the wrapper `agenthub install` wrote for it,
otherwise the interpreter and package path in full. No line in
`CLAUDE.md` is needed: the agent writes `npm run build` as usual, and the hook routes it.

## Hooks

| Hook | What it does |
|---|---|
| `SessionStart` (every `source`) | registers the session: Claude Code's `session_id`, `cwd`, agent `claude-code` and the pid of the Claude Code process, so the hub ends the session when that process exits. Writes `export AGENTHUB_SESSION=<id>` to `CLAUDE_ENV_FILE`, so an `agenthub run` the agent types itself belongs to the session too. |
| `PreToolUse` on `Bash` | classifies the command; a heavy one waits for its grant in the hook and is rewritten to run under it (below) |
| `PostToolUse` (every tool) | passes on news from the hub as `additionalContext` (below) |
| `SessionEnd` | `session.end` within the 1.5 s Claude Code gives these hooks, skipped when no hub is running; the hub notices a dead session through its pidfd anyway |

The pid comes from the hook's ancestry: hooks run as `sh -c` children of the Claude Code process. The
undocumented `CLAUDE_PID` variable is used only when it really is an ancestor; otherwise the nearest `claude`
process, otherwise the parent of the hook's shell. The hook input has no session title, so sessions have none.

Inside subagents the hook input carries `agent_id`, which goes into the run, so `status` shows which subagent
started what.

## Routing heavy commands

A command is heavy when a kind rule of the config matches it ([design §4.1](design.md#41-which-commands-are-heavy));
a light one, or one that already goes through `agenthub run`, is left alone, and so is every call in plan mode.

For a heavy one the hook asks the hub for a run (`hold` 120 s, `wait` 540 s, `notify`) and waits for the grant
inside the hook, so the Bash tool's own timeout covers only the run, not the queue. Command hooks may run 600 s;
the plugin sets that timeout explicitly and gives up after 540 s. Then it returns the same tool input with the
command wrapped — `updatedInput` replaces the whole input, so `description`, `timeout` and `run_in_background`
are kept:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse",
  "updatedInput":{"command":"agenthub run --grant 43 --session a1f3… --shell 'npm run build'",
                  "description":"Build the API","timeout":600000}}}
```

`agenthub run --grant 43` claims the grant the hook left behind (`run.claim`) and runs the original line with
`bash -c` in its scope. If the grant is gone — the permission prompt took longer than 120 s, or was denied and
the call came back later — it queues again as a plain `agenthub run` would.

When the wait runs out the hook denies the call with the hub's reason, and the agent can retry or start the
command in the background (a hub that stays silent 20 s past the wait leaves the call as written):

```
agenthub: waited 540s: needs 3.1 GiB, 1.2 GiB above reserve; retry later or run it in the background
```

A run that can never fit is denied as `agenthub: refused — …`. Anything else — no hub, a hub error, a broken
config — leaves the call as written: the plugin never blocks work by being broken. If Claude Code stops the hook
at its own timeout, the call also goes on unwrapped.

Wrapping the line in `bash -c` has two visible effects: a `cd` inside a heavy command does not change the
directory of later Bash calls, and shell functions and aliases of Claude Code's shell snapshot are not defined
inside it (exported variables are).

### Permissions

Claude Code evaluates permission rules against the **rewritten** command, while the user's rules are written for
the command the agent wrote. A rule `Bash(npm run build:*)` does not match `agenthub run --grant … --shell 'npm
run build'`. The hook therefore judges the command as written against the `Bash(…)` rules Claude Code has: the
settings files (managed, `~/.claude/settings.json`, the project's `.claude/settings.json` and
`.claude/settings.local.json`, as `--setting-sources` selects them; `~/.claude/settings.local.json` for deny and
ask only), and the `--allowedTools`, `--disallowedTools` and `--settings` options read from the Claude Code
process's command line.

| The command as written | The hook |
|---|---|
| could be meant by a `deny` rule — the rule's fixed part appears in it, also with quotes, backslashes and repeated spaces taken out — or cannot be split into its commands while there is a deny rule | leaves the call alone, so Claude Code's own deny applies |
| the same for an `ask` rule | wraps it and answers `ask` |
| every command of the line (split at `&&`, `\|\|`, `;`, `\|`, `\|&`, `&`) matches an `allow` rule | wraps it and answers `allow` |
| anything else | wraps it with no decision: Claude Code decides as it would for any unknown command |

Deny and ask are matched broadly and allow narrowly. A line counts as split only when it holds nothing but simple
commands joined by those operators: `$(…)`, backticks, `<(…)`, heredocs, line breaks, an unquoted `(`, `)`, `\`
or `#`, a backslash inside double quotes or an open quote make it unsplittable. A command with a redirection
other than to `/dev/null` or between streams (`2>&1`) is never answered `allow`. A settings file that cannot be
read leaves the call as written. Claude Code's deny and ask rules still apply to the rewritten command whatever
the hook answers. "Always allow" answers kept for one session are not visible to the hook, so such commands are
asked about again after wrapping.

A rule that allows the wrapper itself (`Bash(agenthub run:*)`, which a "don't ask again" answer to a wrapped
command may save) would allow every heavy command; the hook answers `ask` when such a rule would decide a call
its own judgement leaves open, except in `bypassPermissions` mode.

## News

The hub writes one line per event to `$XDG_RUNTIME_DIR/agenthub/news/<session>`, and the `PostToolUse` hook
moves the file away, reads it and adds the lines next to the tool result. The hook reads a file, not the socket,
so it costs nothing when there is no news and never starts the hub. The file goes away with the session.

Today there is one kind of news: a heavy command whose wait in the hook ran out is remembered for 30 minutes,
and once nothing is queued, admissions are open, its estimate fits and its pools have a free unit, the session
hears `agenthub: room for \`npm run build\` (2.0 GiB) is free now — retry it`. A run the hub stops needs no news:
the reason is in the command's own output, and Claude Code reports a background command that ended.

## Status line

A plugin cannot set the status line, so it is one line in the user's `settings.json`:

```json
{"statusLine": {"type": "command", "command": "agenthub status --line"}}
```

Claude Code passes its JSON on stdin; the command takes `session_id` from it and prints

```
hub ▸ room 9.8 GiB · you p1 · 2 running · 1 queued · queue 3 · paused
```

`room` is the headroom for new runs; `you` is this session's class and runs; `queue` counts every waiting run on
the machine. With no hub running it prints nothing and does not start one. A status line of one's own can call
`agenthub status --line --session <id>` and add the output to its own text.

## Subagent and workflow limits

Claude Code limits concurrency per session (for example `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`). agenthub does not
replace those limits; it adds the part they cannot see — the other sessions on the same machine.
