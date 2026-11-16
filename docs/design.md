# Design

This document describes how agenthub is meant to work. It is a design, not a description of existing code.

## 1. Goals and non-goals

Goals:

1. **One picture of the machine.** Every agent session registers with one hub per user per machine and can see
   who runs what, how much CPU and memory it takes, its priority and its place in the queue.
2. **Priority.** Work proceeds in order of decreasing priority. A lower priority yields CPU automatically and
   waits for memory; a higher priority is never starved by a lower one.
3. **Automatic regulation.** CPU is shared by the kernel through cgroup weights. Memory is handed out by
   admission against a budget, and pressure events shed the least valuable run before the machine thrashes.
4. **Reliability.** A systemd user service with socket activation and a watchdog; state is rebuilt from
   systemd on restart; clients fail open. No cron, no polling loops in clients.
5. **Visibility of decisions.** Every waiting run has a stated reason; every stopped run tells its session why.

Non-goals:

- Managing applications that are not agent work (browser, IDE, chat apps). They are observed and protected by
  the slice layout, never paused or stopped.
- Replacing the last-resort OOM handling (`systemd-oomd`, `earlyoom`). It stays as the final line.
- Sandboxing or security isolation of agent commands.
- Scheduling across machines.

## 2. Components

| Component | Role |
|---|---|
| `agenthubd` | The hub: one process per user, systemd user service, socket-activated. Holds sessions, runs, the queue; decides admission and shedding. |
| `agenthub` | The client CLI: `run`, `status`, `session`, `prio`, `why`, `stop`. Runs commands in the caller's context. |
| systemd units | `agenthub.socket`, `agenthub.service`, `agenthub.slice` and four class slices. |
| Integrations | Claude Code plugin (hooks + status line), later an MCP server for other agents. |

### 2.1 Slice layout

```
user@UID.service
├── app.slice, session.slice          desktop, browser, IDE — untouched
└── agenthub.slice                    CPUWeight=80  MemoryHigh=<agent budget>  MemoryMax=<budget + margin>
    ├── agenthub-p0.slice             CPUWeight=1000  MemoryLow=<protected>   urgent, not preemptible
    ├── agenthub-p1.slice             CPUWeight=400                            high
    ├── agenthub-p2.slice             CPUWeight=100                            normal (default)
    └── agenthub-p3.slice             CPUWeight=25                             background
        └── agenthub-run-<id>.scope   one per admitted run, MemoryHigh=<estimate × 1.5>
```

Weights are hierarchical: the class weights only divide what `agenthub.slice` gets, and `agenthub.slice`
competes with the desktop slices at its own weight. Keeping it below the default 100 means the desktop wins
under contention and agent work gets the whole CPU when the desktop is idle.

`IOWeight` needs the io controller delegated to the user manager, which takes one root drop-in. It is
optional; without it only CPU and memory are regulated.

### 2.2 Why systemd units and not raw cgroupfs

The user manager owns the delegated cgroup tree. Creating runs as transient scopes
(`systemd-run --user --scope --slice=… --unit=agenthub-run-<id>`) keeps systemd's view consistent, gives every
run a unit name the hub can stop over D-Bus, and makes the state discoverable after a hub restart.

## 3. Sessions

A session is one agent instance: a Claude Code session, a Codex session, or any process that registers.

- **Registration:** `agenthub session start --agent claude-code --id <session_id> --pid <agent pid> --cwd <dir>
  [--title <text>]`, called from the agent's start hook.
- **Class:** decided in this order — a runtime override (`agenthub prio`), the first matching project rule in
  the config (path glob → class), the default class (`p2`).
- **Liveness:** the hub opens a pidfd for the agent process, so a crashed session is noticed at once, without
  polling. `agenthub session end` from the agent's end hook is a courtesy, not a requirement.
- **Subagents:** runs carry an optional `agent_id`, so the status shows which subagent of a session started what.

## 4. Runs

A run is one heavy command.

1. The client sends `request` with the command, cwd, session, kind, pools and an optional explicit estimate.
2. The hub answers `grant` (with a run id and the slice), `queued` (position and reason), or `refused`.
3. On `grant` the client starts the command with `systemd-run --user --scope` in the class slice, under the
   run's unit name and `MemoryHigh`, and reports `started` with the scope name.
4. The hub watches the scope's `cgroup.events` (populated → 0) to learn that the run ended, reads
   `memory.peak` and `cpu.stat`, and records them in the history.
5. The client exits with the command's exit code. If the hub stopped the run, the client exits with **75**
   (`EX_TEMPFAIL`) and prints why, so the agent sees the reason in the command's own output.

### 4.1 Which commands are heavy

Commands are classified by rules in the config (regular expression on the command line → kind, default
estimate, pools, class adjustment). Unmatched commands run directly and are not tracked. Examples of kinds:
`build` (`tsc`, `npm run build`, `cargo build`, Gradle), `test` (`jest`, `pytest`, `go test`), `browser`
(Playwright, Puppeteer), `emulator`, `stand` (long-lived dev servers).

### 4.2 Estimates

The expected peak memory of a run comes from, in order: an explicit `--estimate`; the 90th percentile of
`memory.peak` over the last 20 runs with the same fingerprint (program, subcommand, project root); the kind's
default from the config; a global default. Estimates are kept in a SQLite file under `$XDG_STATE_HOME`.

### 4.3 Long-lived runs

Dev servers and test stands are runs of kind `stand`: they hold their memory until stopped and count against
the budget the whole time. They are the first candidates for pools (`stand = 1`) so two sessions do not each
start their own copy.

## 5. Admission

```
reserve   = desktop_reserve                            (config, e.g. 4 GiB or 15 % of RAM)
free      = min(MemAvailable − reserve,
                agent_budget − memory.current(agenthub.slice))
growth    = Σ over running runs of max(0, estimate − memory.current(run))
headroom  = free − growth
grant if  estimate ≤ headroom  and every pool the run needs has a free unit
```

`growth` matters: a build admitted five seconds ago has not reached its peak yet, and without counting what it
is still going to take, the hub would admit three builds into room for one.

Ordering:

- The queue is ordered by **effective class**, then by request time.
- **Aging:** every `aging_step` (default 5 min) of waiting raises the effective class by one, up to `p1`.
  A background run waits, but not forever. Nothing ages into `p0`.
- **No memory backfill:** if the head of the queue does not fit in memory, smaller runs behind it do not jump
  ahead. Otherwise a stream of small runs would starve a large high-priority one.
- **Pool backfill is allowed:** if the head waits for a pool (for example the only emulator), runs that do not
  need that pool may start.

Every `queued` answer carries the reason: `needs 3.1 GiB, 1.2 GiB above reserve`, `pool emulator 1/1 held by
session B`, `memory pressure — admissions paused`.

## 6. Pressure handling

The hub opens PSI triggers on `/proc/pressure/memory` and on `agenthub.slice/memory.pressure`:

| Trigger | Meaning | Action |
|---|---|---|
| `some` ≥ 150 ms per 2 s | tasks are stalling on memory | pause admissions |
| `full` ≥ 50 ms per 2 s, or `MemAvailable` < reserve / 2 | the machine is about to thrash | shed one run |
| no trigger for 30 s and headroom > 0 | calm | resume admissions, wake the queue |

**Shedding** picks one victim: the lowest effective class, then the most recently started run (it has lost
the least work), skipping runs of non-preemptible classes (`p0` by default). The hub writes the reason to the
run's client, stops the scope (`SIGTERM`, then `SIGKILL` after 10 s), records the peak so the next estimate is
higher, and waits a cooldown (10 s) before it may shed again. One victim at a time: stopping one build usually
ends the pressure, and stopping three would throw away work for nothing.

**Freezing is not used for memory.** A frozen cgroup keeps its memory; pausing a run does not relieve memory
pressure. Freezing is only a possible future option for CPU-bound background runs.

**CPU is left to the kernel.** Weights already give the right share under contention. The hub only reports CPU
pressure; it does not regulate CPU in user space.

## 7. Visibility

`agenthub status` prints the machine and the queue:

```
machine   31.0 GiB  available 9.8 GiB  reserve 4.0 GiB  psi mem some 2 % full 0 %  cpu 63 %
SESSION     AGENT        CLASS  PROJECT         RUNS
a1f3…       claude-code  p1     ~/work/api      2
9c02…       claude-code  p3     ~/hobby/game    0 (1 queued)
RUN   SESSION  CLASS  KIND     MEM now/peak/est     CPU   AGE    COMMAND
41    a1f3…    p1     build    1.9/2.3/2.5 GiB      310%  1m12s  npm run build
42    a1f3…    p1     test     0.8/0.8/1.4 GiB      95%   0m20s  jest src/orders
QUEUE 43  9c02…  p3  browser  est 1.5 GiB  waiting 2m: pool browser 2/2 held (41, 42)
```

`agenthub why <run>` explains one decision in full. `agenthub status --json` feeds status lines and dashboards.

## 8. Reliability and failure modes

- `agenthub.socket` (`ListenStream=%t/agenthub.sock`, `SocketMode=0600`) activates `agenthub.service`
  (`Type=notify`, `WatchdogSec=30`, `Restart=on-failure`, its own small `MemoryMax`). Connections made while the
  hub restarts are queued by the socket, not refused.
- On start the hub rebuilds runtime state from systemd: every `agenthub-run-*.scope` plus the metadata file the
  client wrote under `$XDG_RUNTIME_DIR/agenthub/runs/`. Sessions re-register on their next request.
- The hub exits after 10 minutes with no sessions and no runs; the socket brings it back.

| Failure | Behaviour |
|---|---|
| Hub not reachable within 1 s | client runs the command directly in its class slice, prints a warning; CPU weights still apply |
| `systemd-run` not available | client runs the command as a plain child process |
| Hub crashes during runs | runs keep going (they belong to systemd, not the hub); state rebuilt on restart |
| Client killed while queued | hub sees the socket close and drops the request |
| Client killed while running | the scope lives on until the command ends; the hub still accounts it |
| Agent session crashes | pidfd closes; queued requests dropped; running runs finish |
| PSI triggers unavailable (kernel < 6.5) | hub samples pressure files every 2 s while any run is active |
| Config error | hub refuses to start and says where; clients fail open |

## 9. Security

- Single user, no root, no network. The socket is mode 0600 in `$XDG_RUNTIME_DIR`, and the hub checks
  `SO_PEERCRED` so only the same uid can talk to it.
- The hub never executes commands. It only grants, observes, and stops units named `agenthub-run-*` that a
  client of the same user registered.
- Command lines are stored in the local history for estimates; they may contain secrets passed on the command
  line, so the history file is mode 0600 and can be limited to the program and subcommand.

## 10. Configuration

`$XDG_CONFIG_HOME/agenthub/config.toml`:

```toml
[machine]
desktop_reserve = "4G"      # never handed to agent work
agent_budget = "auto"       # RAM minus desktop_reserve
aging_step = "5m"

[classes]
p0 = { cpu_weight = 1000, memory_low = "2G", preemptible = false }
p1 = { cpu_weight = 400 }
p2 = { cpu_weight = 100 }
p3 = { cpu_weight = 25 }

[[projects]]
path = "~/work/**"
class = "p1"

[[projects]]
path = "~/hobby/**"
class = "p3"

[pools]
browser = 2
emulator = 1
stand = 1

[[kinds]]
name = "build"
match = '(^|\s)(tsc|cargo build|gradle\w*|(npm|pnpm|yarn) (run )?build)\b'
estimate = "2G"

[[kinds]]
name = "browser"
match = 'playwright|puppeteer'
estimate = "1.5G"
pools = ["browser"]
```

## 11. Language and size

Python 3.11+ with the standard library only (`asyncio`, `socket`, `sqlite3`, `tomllib`), talking to systemd
through `systemd-run`/`systemctl` and to the kernel through cgroupfs and `/proc/pressure`. The target is a hub
small enough to read in one sitting. A port to a compiled language stays possible because the protocol is
plain JSON lines.

## 12. Testing

- **Scheduler core as pure functions:** admission, ordering, aging, backfill and victim choice are tested with a
  fake clock and fake memory readings.
- **Integration on a real user manager:** synthetic memory hogs in a test slice with a small budget; checks
  that admission holds, PSI shedding fires, the victim is the right one, weights apply.
- **Edge matrix:** hub down, hub restart mid-run, client killed while queued and while running, session crash,
  config error, kernel without PSI triggers, pool exhausted, estimate far too low, run that never ends,
  concurrent requests racing for the last free memory, two sessions with the same class.
