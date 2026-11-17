# agenthub

**One load hub for every AI coding agent on a Linux machine.** Sessions register with the hub, every heavy
command (build, test suite, headless browser, emulator, dev stand) asks it for room, work runs in order of
priority, and the kernel shares CPU between priorities by weight. Nobody needs a cron job, and a session that
never heard of the others still cannot sink the machine.

> **Status: milestones M1–M3 work.** Sessions, a priority queue with admission by memory, class slices, a
> fail-open client, PSI-driven pausing and shedding, learned estimates, pools, aging, and a Claude Code plugin
> that routes builds and tests through the hub run on a real systemd user manager. Other agents come next — see
> [docs/roadmap.md](docs/roadmap.md).
> На русском — [README.ru.md](README.ru.md).

## The problem

Run four to fifteen agent sessions on one machine (Claude Code, Codex, subagents, workflows) and each of them
decides on its own to start `tsc`, `jest`, `cargo build`, Gradle, Playwright or an emulator. None of them knows
what the others are doing. The result is the same everywhere:

- memory runs out, swap fills, the desktop freezes for minutes, then the OOM killer (often `systemd-oomd`)
  takes out the whole graphical session — terminals, browser, IDE and every agent with them;
- every build runs slower than if they had run one after another;
- nobody can see who is running what, or why their run is slow.

Agent tools only limit concurrency **inside one session**. Across sessions, people keep writing the same
semaphore script ([prior art](docs/prior-art.md)). None of the existing ones combines priorities, memory-aware
admission and a live picture of the machine on Linux.

## How it works

```
 session A (p1) ─┐                          ┌─ agenthub.slice (CPUWeight, MemoryHigh = agent budget)
 session B (p2) ─┼─ unix socket ─ agenthubd ─┤   ├─ agenthub-p0.slice  weight 1000, MemoryLow
 session C (p3) ─┘   register,               │   ├─ agenthub-p1.slice  weight  400
                     ask for room,           │   ├─ agenthub-p2.slice  weight  100
                     status                  │   └─ agenthub-p3.slice  weight   25
                                             └─ each run = its own scope in its class slice
```

1. **The kernel does the CPU part.** Each priority class is a systemd slice with its own `CPUWeight`. Under
   contention a lower class yields automatically; when the machine is idle it gets everything. No polling.
2. **The hub does the memory part.** Memory cannot be shared by weight, so a run is admitted only when its
   expected peak (learned from earlier runs of the same command) fits above the desktop reserve. The rest wait
   in one queue ordered by class, then by age.
3. **Pressure is an event, not a poll.** The hub subscribes to kernel PSI triggers (pressure stall
   information). When memory stalls, it stops admitting; if stalls get serious, it stops the newest run of the
   lowest class and tells its session why — before the kernel or `systemd-oomd` picks a victim at random.
4. **Everyone sees the same picture.** `agenthub status` (and a status line in each agent) shows sessions,
   their runs, CPU and memory per run, the queue and the reason each waiting run waits.
5. **It fails open.** If the hub is down, a command still runs — directly in its class slice, so CPU weights
   keep working. The hub never blocks work by being broken.
6. **It never runs commands itself.** The client runs the command in the caller's own context; the hub only
   grants, observes and stops scopes it was told about.

## Principles

- Kernel mechanisms first (cgroup v2 weights, memory protection, PSI); user space only decides admission.
- No root. Runs under the user's systemd manager; an optional root step only adds disk I/O weights.
- Event-driven: socket activation, PSI triggers, pidfds. No cron, no sleep loops in clients.
- Agent-neutral core; Claude Code is the first integration (hooks + status line), others via the CLI or MCP.
- Small and boring: Python 3.11+ standard library only.

## Documents

| Document | What is in it |
|---|---|
| [docs/design.md](docs/design.md) | Architecture, priority model, admission, pressure handling, failure modes, security, testing |
| [docs/protocol.md](docs/protocol.md) | The socket protocol between clients and the hub |
| [docs/claude-code.md](docs/claude-code.md) | Claude Code integration: hooks, command rewriting, status line, permissions |
| [docs/prior-art.md](docs/prior-art.md) | Existing tools and issues, and why none of them covers this |
| [docs/roadmap.md](docs/roadmap.md) | Milestones and open questions |

## Requirements

Linux with cgroup v2 and a systemd user manager (cpu and memory delegated — the default on current distros),
kernel 6.5+ for unprivileged PSI triggers (older kernels fall back to 2-second sampling), Python 3.11+.

## License

[MIT](LICENSE)

## Try it

```sh
git clone https://github.com/Prime-GS/agenthub && cd agenthub
python3 -m agenthub install                  # user units + ~/.local/bin/agenthub, no root
agenthub session start demo --cwd .          # optional, ends with this shell; without it the shell is the session
agenthub session prio demo p1
agenthub run --session demo --estimate 2G -- npm run build
agenthub status                              # machine, sessions, runs, queue with reasons
agenthub why 3                               # one decision in full
agenthub run --pool browser -- npx playwright test   # needs [pools] browser = N in the config
agenthub admissions pause --reason demo      # hold new runs (running ones go on); resume lifts it
agenthub uninstall
```

With Claude Code, start sessions with the plugin and nothing else changes — builds and tests the agent runs wait
for room in the hub ([docs/claude-code.md](docs/claude-code.md)):

```sh
claude --plugin-dir ./claude-code
```

Configuration lives in `~/.config/agenthub/config.toml` ([design §10](docs/design.md#10-configuration)); without
it the hub keeps 15 % of RAM for the desktop and gives the rest to agent work. Tests:
`python3 -m unittest discover -s tests`; `AGENTHUB_IT=1` adds the tests against the real user manager.
