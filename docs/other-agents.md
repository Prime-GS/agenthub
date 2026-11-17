# Other agents

Claude Code has a plugin ([claude-code.md](claude-code.md)). Any other agent — Codex CLI, a plain terminal, a
script, any MCP client — reaches the same queue in one of two ways:

| Way | For | What routes the commands |
|---|---|---|
| `agenthub wrap -- <agent>` | an agent that runs shell commands (Codex CLI, a shell, Aider, a script) | shims for the build and test tools, first in the agent's `PATH` |
| `agenthub mcp` | an agent that can call MCP tools | the agent calls the `run` tool instead of its shell |

Both make one session per agent instance, next to the Claude Code sessions, under the same classes, budget and
queue. Nothing in the hub protocol is specific to them: the session's `agent` field is a free name.

## `agenthub wrap`

```sh
agenthub wrap -- codex                      # an interactive Codex session
agenthub wrap --agent build-bot -- ./ci.sh  # a script
agenthub wrap -- bash                       # a terminal whose builds wait for room
```

The wrapper registers a session named `<agent>-<12 hex digits>` (the agent name is the program's name or
`--agent`, with characters other than ASCII letters, digits and `._-` turned into `-`, at most 40), with its own pid and working directory, and then becomes the
agent: `exec` keeps the pid, so the hub ends the session when the agent exits. The agent gets
`AGENTHUB_SESSION` (an `agenthub run` it types belongs to the session) and, first in `PATH`, the directory
`$XDG_RUNTIME_DIR/agenthub/shims` with one small script per tool:

`make cmake ninja gradle mvn npm pnpm yarn bun npx bunx tsc webpack rollup vite next nest jest vitest mocha
playwright cypress pytest uv poetry rspec phpunit go cargo`

A shim looks at the command it was called as (`npm run build`, `cargo test`):

- a kind rule of the config matches it ([design §4.1](design.md#41-which-commands-are-heavy)) → it goes through the
  hub as `agenthub run` would: waits for room, runs in its scope, the hub learns its peak; the hub picks the kind
  from its own config;
- no rule matches (`npm install`, `go version`) → the real tool runs directly, without asking the hub;
- it runs inside a run already (`AGENTHUB_RUN` is set: `make` calling `cargo`, `npm run build` calling `tsc`) →
  the real tool runs directly; the outer run's grant covers it;
- the tool is not installed → `<tool>: command not found`, exit 127.

The real tool is the first one in `PATH` that is not a shim, and it gets the agent's environment as it was
(`PYTHONPATH` included: agenthub itself runs isolated) and default signal handling. Interpreters (`python3`, `node`) get no shim: each
of their many calls would start one more interpreter. So these pass the shims by:

- a command started by its path: `./gradlew build`, `node_modules/.bin/jest`, `python3 -m pytest`;
- a shell that reorders `PATH` after start (`nvm use`, a login profile that prepends its own directories);
- children of a run that outlive it (a dev server started by `npm run build`): they keep `AGENTHUB_RUN`, so
  their later builds run directly.

`agenthub run -- <command>` routes any of them by hand.

When the hub cannot be reached the agent still starts (`agenthub: hub not reachable …; heavy commands run
directly`), and every heavy command runs directly with the same warning, as `agenthub run` does. If the hub
was away when the agent started, the first heavy command registers the session, with the agent's pid and name
(`AGENTHUB_SESSION_PID`, `AGENTHUB_AGENT`), so it still ends with the agent.

### Codex CLI

Checked with Codex CLI 0.165: Codex keeps the shim directory ahead of the system directories in the commands it
runs (it only puts its own helper directories in front), passes `AGENTHUB_SESSION` on, and its commands are
children of the `codex` process. Codex's sandbox decides whether a shim can reach the hub: the hub's socket and
the user manager's D-Bus socket are unix sockets under `$XDG_RUNTIME_DIR`, and where the sandbox blocks them
the shim fails open — the command runs, but outside the queue. Outside the sandbox (`-s danger-full-access`)
the queue works as with Claude Code.

Codex also has hooks, but a `PreToolUse` hook may rewrite a command only together with
`permissionDecision: "allow"`, which would approve every rewritten command past Codex's own approval rules. The
wrapper changes nothing about approvals: Codex asks for the same commands as before.

### Every start wrapped

To make plain `codex` (or any other agent command) start wrapped, put a script of that name in a directory that
comes before the real one in `PATH`, such as `~/.local/bin/codex`:

```sh
#!/bin/sh
# agenthub: Codex CLI starts as an agenthub session; delete this file to start Codex directly again
self=$(readlink -f "$0")
real=
IFS=:
for dir in $PATH; do
    [ -n "$dir" ] && [ -x "$dir/codex" ] || continue
    [ "$(readlink -f "$dir/codex")" = "$self" ] && continue
    real="$dir/codex"
    break
done
unset IFS
[ -n "$real" ] || { echo "codex: command not found (agenthub wrapper: no other codex on PATH)" >&2; exit 127; }
# inside a run of the hub its grant covers it; without agenthub there is nothing to join
if [ -n "${AGENTHUB_RUN:-}" ] || ! command -v agenthub >/dev/null 2>&1; then
    exec "$real" "$@"
fi
exec agenthub wrap --agent codex -- "$real" "$@"
```

It skips itself by its resolved path, so the real `codex` is the next one in `PATH`, started by its full path
(no loop). It covers whatever starts `codex` through `PATH`; an IDE extension or a desktop app that starts its
own copy of Codex, and a shell that puts another `codex` first (`nvm use` of another Node version), pass it by.

## `agenthub mcp`

An MCP server on stdio. Codex:

```toml
# ~/.codex/config.toml
[mcp_servers.agenthub]
command = "agenthub"
args = ["mcp"]
tool_timeout_sec = 1800   # a build may wait in the queue before it runs
```

Claude Desktop, Cursor and others take the same command in their own MCP settings.

The server has one tool, `run`, with `command` (a shell line, run with `bash -c`) and an optional `cwd` (default:
the server's working directory). It runs `agenthub run --shell <command>` as a child process: the command waits
in the queue, runs in its scope, and the answer is `exit <code>` followed by the last 200 lines (at most 16 KiB)
of its output, stdout and stderr together, with the hub's own notes (`agenthub: queued #1 …`) among them. A
cancelled call (`notifications/cancelled`) sends TERM to the command's whole process group — it leaves the queue,
or the command and its children stop — and gets no answer. The command's stdin is `/dev/null`; output is read
until the command ends and one second more, so a child it leaves running in the background cannot hold the
answer.

On `initialize` the server registers a session `mcp-<client name>-<12 hex digits>` (the name from the client's
`clientInfo`), with the pid of the process that started it — the agent. It ends the session when the client
closes stdin; the hub also ends it when the agent exits. Under `agenthub wrap` the server uses the wrapper's
session instead of making a second one.

Unlike a shim, the `run` tool always goes through the hub, as an explicit `agenthub run` does: the agent chose
it for a heavy command.

MCP clients stop a tool call after their own timeout (Codex: `tool_timeout_sec`, 60 s by default). Waiting in
the queue counts, so the timeout has to cover the wait plus the run.

### Security

The `run` tool executes any shell command the agent passes, as the user, outside the agent's sandbox and
outside the approval rules the agent applies to its own shell commands. It is as powerful as giving the agent an
unrestricted shell. The tool is marked as not read-only and destructive, so clients that ask before such MCP
calls ask before each run; enable the server only for agents you would also let run shell commands without
asking.
