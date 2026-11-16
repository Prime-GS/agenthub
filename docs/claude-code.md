# Claude Code integration

Claude Code is the first agent agenthub integrates with. The integration is a plugin made of hooks and a status
line; it relies only on documented hook behaviour ([hooks reference](https://code.claude.com/docs/en/hooks)).

## Hooks

| Hook | What it does |
|---|---|
| `SessionStart` (`startup`, `resume`, `clear`, `compact`, `fork`) | `agenthub session start` with `session_id`, `cwd`, the Claude Code pid and the session title |
| `PreToolUse` on `Bash` | classifies the command; a heavy one is rewritten to run through `agenthub run` (below) |
| `SessionEnd` | `agenthub session end` (best effort — SessionEnd hooks share a 1.5 s budget, and the hub notices a dead session through its pidfd anyway) |
| `PostToolUse` | when the hub has news for this session (a run of it was shed, a queue it waited on cleared), adds one line through `additionalContext` |

Hook input carries `session_id`, `cwd`, `tool_input` and, inside subagents, `agent_id` — enough to attribute
every run to a session and a subagent.

## Routing heavy commands

`PreToolUse` hooks may return `updatedInput`, which **replaces the whole tool input** before execution. The hook
returns the same input with the command wrapped:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse",
  "updatedInput":{"command":"agenthub run --session a1f3… -- npm run build","description":"Build the API","timeout":600000}}}
```

The agent does not have to remember any rule in `CLAUDE.md`; a command it writes the usual way still goes
through the hub. Light commands are passed through untouched.

### Where the waiting happens

A queued run waits somewhere, and both places have limits:

- **Inside the Bash tool call** — `agenthub run` blocks until granted. The Bash tool's timeout (at most
  10 minutes) covers queue time plus run time, so a long wait can eat the build's own time.
- **Inside the PreToolUse hook** — command hooks may run up to 600 s by default. The hook can ask for the slot,
  wait for the grant there, and rewrite the command to `agenthub run --grant <id> -- …`, so the Bash timeout
  covers only the run.

The plan is the second: wait in the hook, run under the grant. If the wait passes its limit, the hook denies the
call with the reason (`queued 2nd: needs 3.1 GiB, 1.2 GiB above reserve`), and the agent can retry later or start
the command in the background.

### Permissions

Claude Code evaluates permission rules against the **rewritten** command. Two consequences:

- The hook must not answer `allow` on its own — that would approve a command the user's rules would have asked
  about. It returns `updatedInput` with no decision, so the usual rules apply.
- Rules written for the plain command (`Bash(npm run build:*)`) do not match `agenthub run -- npm run build`.
  Options under consideration: mirror the user's Bash rules with the `agenthub run … --` prefix at install time,
  or let the hook answer `ask` only when the original command was not allowed. This is open question 1 in the
  [roadmap](roadmap.md).

## Status line

`agenthub status --json --session <id>` returns a compact line for Claude Code's status line:

```
hub ▸ mem 9.8 GiB free · you p1: 2 running · queue 1 (p3, 2m)
```

## Subagent and workflow limits

Claude Code limits concurrency per session (for example `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`). agenthub does not
replace those limits; it adds the part they cannot see — the other sessions on the same machine.
