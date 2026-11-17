# Roadmap

| Milestone | Content | Done when |
|---|---|---|
| **M0 — design** | These documents | Published |
| **M1 — core** (done) | `agenthubd` with sessions, runs, queue, admission by memory, class slices, fail-open client, `status`/`why`; systemd units; installer for the user manager | Two sessions with different classes on a real machine: the lower one waits for memory, both get CPU by weight; hub restart mid-run loses nothing |
| **M2 — pressure** (done) | PSI triggers, shedding, learned estimates (SQLite history), pools, aging | A synthetic memory storm sheds exactly one lowest-class run and the desktop stays responsive |
| **M3 — Claude Code plugin** (done) | SessionStart/PreToolUse/SessionEnd/PostToolUse hooks, waiting in the hook, status line | Several real sessions run builds and tests through the hub without any instruction in `CLAUDE.md` |
| **M4 — other agents** | MCP server for agents without hooks, wrapper for Codex CLI and plain terminals | A non-Claude agent shares the same queue |
| **M5 — optional root extras** | io controller delegation for `IOWeight`, desktop memory protection | Disk-heavy runs stop starving the desktop |

## Open questions

1. ~~Permission rules and rewritten commands.~~ Settled in M3: the hook judges the command as written against the
   user's rules — see [claude-code.md](claude-code.md#permissions).
2. **First run of an unknown command.** With no history, the kind default may be far off. Start such runs with a
   cautious `MemoryHigh` and learn from the first peak?
3. **Long-lived stands.** How should a dev server that runs for hours be charged — its current memory, its peak,
   or a reservation the session declares?
4. **Interplay with `systemd-oomd`.** Should `agenthub.slice` get its own `ManagedOOMMemoryPressure` limit so the
   kernel-side last resort also targets agent work before the desktop?
5. **Heavy commands nobody classified.** Detect them after the fact (a light command whose cgroup grew past a
   threshold) and suggest a rule?
