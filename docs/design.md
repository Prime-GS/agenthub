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
| `agenthubd` | The hub (`agenthub hub`): one process per user, systemd user service, socket-activated. Holds sessions, runs, the queue; decides admission and shedding. |
| `agenthub` | The client CLI: `run`, `status`, `why`, `stop`, `admissions`, `session start\|end\|prio`, `hook`, `wrap`, `mcp`, `install`, `uninstall`. Runs commands in the caller's context. |
| systemd units | `agenthub.socket`, `agenthub.service`, `agenthub.slice` and four class slices. |
| Integrations | Claude Code plugin in `claude-code/` (hooks; `agenthub status --line` for the status line); `agenthub wrap` (shims in `PATH`) and `agenthub mcp` (an MCP server) for other agents — [other-agents.md](other-agents.md). |

### 2.1 Slice layout

```
user@UID.service
├── app.slice, session.slice          desktop, browser, IDE — untouched
└── agenthub.slice                    CPUWeight=80  MemoryHigh=<agent budget>  MemoryMax=<budget + margin>
    ├── agenthub-p0.slice             CPUWeight=1000  MemoryLow=<protected>   urgent, not preemptible
    ├── agenthub-p1.slice             CPUWeight=400                            high
    ├── agenthub-p2.slice             CPUWeight=100                            normal (default)
    └── agenthub-p3.slice             CPUWeight=25                             background
        └── agenthub-run-<id>.scope   one per admitted run, MemoryHigh=<estimate × 1.5, at least 256 MiB>
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

- **Registration:** `agenthub session start <session_id> --agent claude-code --pid <agent pid> --cwd <dir>
  [--title <text>]`, called from the agent's start hook. Without `--pid` the calling shell is the session's
  process; `--pid 0` makes a session that lives until `agenthub session end`.
- **Implicit session:** `agenthub run` without `--session` (and without `$AGENTHUB_SESSION`) registers the
  calling shell as session `pid-<shell pid>`, so a plain terminal works with no setup.
- **Class:** decided in this order — a runtime override (`agenthub session prio <id> <class>`), the first
  matching project rule in the config (path glob → class), the default class (`p2`). The override survives the
  session registering again.
- **Liveness:** the hub opens a pidfd for the agent process, so a crashed session is noticed at once, without
  polling. `agenthub session end` from the agent's end hook is a courtesy, not a requirement.
- **Other agents:** `agenthub wrap -- <agent>` registers `<agent>-<12 hex digits>` with its own pid and then
  becomes the agent (`exec`), so the session ends with it; `agenthub mcp` registers `mcp-<client name>-<12 hex
  digits>` with the pid of the process that started it, or uses `$AGENTHUB_SESSION` under a wrapper. An explicit
  session the hub does not know is registered by `agenthub run` with `$AGENTHUB_AGENT` and
  `$AGENTHUB_SESSION_PID` when they are set.
- **Subagents:** runs carry an optional `agent_id`, so the status shows which subagent of a session started what.
- **Persistence:** sessions are written to `$XDG_RUNTIME_DIR/agenthub/sessions/`; after a hub restart those
  whose process is still alive come back with their class and override.

## 4. Runs

A run is one heavy command.

1. The client sends `request` with the command, cwd, session, kind, pools and an optional explicit estimate.
2. The hub answers `grant` (with a run id and the slice), `queued` (position and reason), or `refused`; on
   `refused` the client exits with **69** (`EX_UNAVAILABLE`) without running the command.
3. On `grant` the client starts the command with `systemd-run --user --scope` in the class slice, under the
   run's unit name and `MemoryHigh`, and reports `started` with the scope name.
4. The hub watches the scope's `cgroup.events` (populated → 0, through inotify) to learn that the run ended,
   whoever started it. It reads the scope's `memory.peak` and `cpu.stat` every 2 s while the run lives and once
   more at its end, so the peak survives systemd removing the scope first, and writes one history row when the
   run becomes ended (§4.2).
5. The client exits with the command's exit code. If the hub stopped the run, or the run's `wait` passed in
   the queue, the client exits with **75** (`EX_TEMPFAIL`) and prints why, so the agent sees the reason in the
   command's own output. Signals sent to the client (TERM, INT, HUP) are passed on to the command.

### 4.1 Which commands are heavy

Commands are classified by rules in the config (regular expression on the command line → kind, default estimate,
pools; a class adjustment per kind is planned, not implemented). `agenthub run --kind NAME` names the kind
instead, and `--pool NAME` (repeatable) replaces the kind's pools. A command an integration hook sees that matches
no rule runs directly and is not tracked; an explicit `agenthub run` is always tracked, as kind `other` when no
rule matches. Examples of kinds: `build` (`tsc`, `npm run build`, `cargo build`, Gradle), `test` (`jest`,
`pytest`, `go test`), `browser` (Playwright, Puppeteer), `emulator`, `stand` (long-lived dev servers).

A config without a `kinds` key gets three built-in kinds, tried in this order: `browser` (Playwright, Puppeteer,
`cypress run`; 1.5 GiB), `test` (`jest`, `vitest`, `pytest`, `mocha`, `rspec`, `phpunit`, `go test`, `cargo test`,
`python -m pytest|unittest`, `npm|pnpm|yarn|bun [run] test`, `gradle|mvn test`; 1.5 GiB) and `build` (`tsc`,
`webpack`, `rollup`, `vite build`, `next build`, `cargo build`, `go build`, Gradle, Maven, `make`, `cmake --build`,
`ninja`, `npm|pnpm|yarn|bun [run] build`; 2 GiB). Without them every command would be light and the Claude Code
plugin would route nothing. The tool has to stand where a command starts — at the line start or after `;`, `&&`,
`||`, `|`, `(`, past `VAR=value` assignments, a wrapper (`env`, `nice`, `nohup`, `time`, `timeout N`, `npx`,
`uv run`, `bash -c`) and a path — so `git commit -m 'fix make'` or `pip install pytest` stay light; a script name
after it may follow a colon (`npm run test:unit`). `docker build` is left out: its memory is charged to the
Docker daemon, not to the run's scope. A `[[kinds]]` list of one's own replaces the built-in one; `kinds = []`
turns kinds off.

The same rules decide for the shims of `agenthub wrap` whether a tool's command goes through the hub. A command
started inside a run (the client sets `AGENTHUB_RUN` for it) never queues again: `make` calling `cargo` would
otherwise wait behind the grant its own parent holds.

### 4.2 Estimates

The expected peak memory of a run comes from, in order: an explicit `--estimate`; the 90th percentile of
`memory.peak` over the last 20 runs with the same fingerprint; the kind's default from the config; the global
`default_estimate` (1 GiB). `status` and `why` show which one was used (`explicit`, `history`, `kind build`,
`default`).

- **Fingerprint:** program, subcommand and project root, e.g. `npm run build @ /home/u/work/api`. The program is
  found after `VAR=value` prefixes and wrappers (`env` with its options, `npx`, `time`, `nohup`, `timeout N`,
  `taskset -c LIST`, `nice -n N`, `ionice -c N`), with redirections (`> log`, `2>&1`) left out; the subcommand is
  the first word that is not an option (two for `run X` and `exec X`); the project root is the nearest directory
  upwards that holds `.git`, else the cwd. For a shell line (`--shell`, `sh -c`) it is the last command after `;`,
  `&&`, `||` that is not `cd`, and the first stage of a pipeline.
- **Shed runs** count as their peak × 1.5: a run stopped under pressure needed more than it reached.
- A learned estimate is capped at the agent budget, so history alone never makes a command refused.
- **History file:** `$XDG_STATE_HOME/agenthub/history.sqlite3` (mode 0600). One row per ended run whose peak
  is known: fingerprint, class, kind, estimate, peak, CPU seconds, duration, shed or not; the last 50 per
  fingerprint are kept. The exit code is not stored: it arrives after the run has ended. A broken or locked
  file costs only the learned estimates. `agenthub uninstall` leaves the file and prints its path.

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
grant if  admissions are open  and  estimate ≤ headroom  and  every pool the run needs has a free unit
```

A run whose estimate is above the whole `agent_budget` is refused at once instead of waiting forever. The hub
decides again on every run end and every 2 s while anything waits.

`growth` matters: a build admitted five seconds ago has not reached its peak yet, and without counting what it
is still going to take, the hub would admit three builds into room for one.

Ordering:

- The queue is ordered by **effective class**, then by request time.
- **Aging:** every `aging_step` (default 5 min) of waiting raises the effective class by one, up to `p1`.
  A background run waits, but not forever. Nothing ages into `p0`.
- **No memory backfill:** if the head of the queue does not fit in memory, smaller runs behind it do not jump
  ahead. Otherwise a stream of small runs would starve a large high-priority one.
- **Pool backfill is allowed:** if the head waits for a pool (for example the only emulator), runs that do not
  need that pool may start. A unit is held from grant to end; runs behind it that need the same pool wait
  behind it.
- Aging changes only the place in the queue; the run still starts in the slice of its own class, and a running
  run is shed by its own class.

Every `queued` answer carries the reason, the first that applies: a pause (`memory pressure — admissions
paused`, `admissions paused by hand (…)`), memory (`needs 3.1 GiB, 1.2 GiB above reserve`, or `behind run 41
(p1, needs 2.0 GiB)` for the runs behind it), a pool (`pool emulator 1/1 held by run 41`, or `behind run 43 for
pool emulator`).

## 6. Pressure handling

The hub opens PSI triggers on `agenthub.slice/memory.pressure` and on `/proc/pressure/memory` (the second can
be turned off with `machine.system_pressure = false`). It also reads the `total` stall times of the same files
every 2 s and acts on the difference, which is all it has when the kernel refuses triggers (before 6.5, or a
file it may not write) and catches what a trigger missed while the slice's cgroup did not exist:

| Stall | Meaning | Action |
|---|---|---|
| `some` ≥ 150 ms per 2 s | tasks are stalling on memory | pause admissions |
| `full` ≥ 50 ms per 2 s, or `MemAvailable` < reserve / 2 | the machine is about to thrash | shed one run |
| none for 30 s and headroom > 0 | calm | resume admissions, wake the queue |

A stall counts only when memory is actually short: agent work uses at least 90 % of its budget, or
`MemAvailable` is below the reserve. A run that outgrows its own `MemoryHigh` on an otherwise roomy machine
stalls itself, and stopping other runs would not help it. `MemAvailable` below half the reserve sheds on its
own.

The pause shows as the reason of every queued run and in `status`. `agenthub admissions pause|resume` sets
and lifts a pause by hand; it is kept in the state directory, survives a hub restart and does not lift itself.
A pressure pause is not kept: after a restart the hub finds the pressure again.

**Shedding** picks one victim among the running runs: the lowest class, then the most recently started run (it has
lost the least work), skipping runs of non-preemptible classes (`p0` by default), runs already being stopped, and
runs the hub cannot stop (no scope and no client) or whose scope is already empty. The hub tells the run's client
why (`shed: memory full-pressure 74 ms/2 s (agents); lowest class, newest run`), stops the scope the same way as
`run.stop` (`SIGTERM`, then `SIGKILL` after 10 s through the scope's `TimeoutStopSec`), marks the run shed so its
history row raises the next estimate, and waits a cooldown (10 s) before it may shed again, and longer while the
victim is still alive (up to 20 s: a run that ignores `SIGTERM` keeps its memory until the `SIGKILL`). One victim
at a time: stopping one build usually ends the pressure, and stopping three would throw away work for nothing.
With no candidate the hub only pauses.

**Freezing is not used for memory.** A frozen cgroup keeps its memory; pausing a run does not relieve memory
pressure. Freezing is only a possible future option for CPU-bound background runs.

**CPU is left to the kernel.** Weights already give the right share under contention. The hub only reports CPU
pressure; it does not regulate CPU in user space.

## 7. Visibility

`agenthub status` prints the machine and the queue:

```
machine  31.0 GiB  available 9.8 GiB  reserve 4.0 GiB  budget 27.0 GiB  agents 2.7 GiB  headroom 4.1 GiB
pressure memory some 2% full 0%  agents some 5% full 0%  cpu some 63%  admissions open
pools    browser 2/2  emulator 0/1
SESSION          AGENT        CLASS WHY                        CWD
a1f3…            claude-code  p1    project rule ~/work/**     ~/work/api
9c02…            claude-code  p3    project rule ~/hobby/**    ~/hobby/game
RUN   SESSION          CLASS KIND     MEM now/est             CPU     AGE  COMMAND
41    a1f3…            p1    build    1.9 GiB/2.5 GiB        310%   1m12s  npm run build
42    a1f3…            p1    browser  819 MiB/1.4 GiB         95%   0m20s  npx playwright test
QUEUE #1 run 43  p3→p2  browser  est 1.5 GiB (history)  waiting 6m03s: pool browser 2/2 held by run 41, 42  ·  npx playwright test
```

`p3→p2` is a run that aged one class while it waited.

`agenthub why <run>` explains one decision in full. `agenthub status --json` feeds dashboards; `agenthub status
--line` prints one short line for an agent's status line (`hub ▸ room 9.8 GiB · you p1 · 2 running · queue 3`) and
prints nothing when no hub is running — it checks the hub's pid file next to the socket first (pid and start
time, so a file left by a stopped hub names no live hub), so a status line that refreshes often never starts the
hub through socket activation. It reads Claude Code's JSON from stdin for at most 1 s. A session that gave up waiting is told
when the room it needed appears, through a news file its integration reads ([claude-code.md](claude-code.md#news)).

## 8. Reliability and failure modes

- `agenthub.socket` (`ListenStream=%t/agenthub.sock`, `SocketMode=0600`) activates `agenthub.service`
  (`Type=notify`, `WatchdogSec=30`, `Restart=on-failure`, its own small `MemoryMax`). Connections made while the
  hub restarts are queued by the socket, not refused.
- On start the hub rebuilds runtime state from cgroupfs and its state directory: every populated
  `agenthub-run-*.scope` plus the metadata file the hub wrote under `$XDG_RUNTIME_DIR/agenthub/runs/`, the
  sessions whose process is alive, and the run-id counter. A scope without metadata is still accounted, as an
  unknown run. A request that was queued is queued again by its client (see below).
- The hub exits after `idle_exit` (10 minutes by default) with no sessions, no runs and no connections; the
  socket brings it back.

| Failure | Behaviour |
|---|---|
| Hub not reachable within 1 s, or silent for 5 s after accepting | client runs the command directly in its class slice, prints a warning; CPU weights still apply |
| A Claude Code hook fails (no hub, broken config, unreadable settings, any error) | the hook prints nothing and exits 0: Claude Code runs the command as written |
| A grant a hook waited for is not claimed (permission prompt denied or slow) | the grant is held for its `hold` (120 s), then dropped; a late `agenthub run --grant` queues again |
| A shim of `agenthub wrap` or the MCP `run` tool cannot reach the hub | the command runs directly with the warning above; `wrap` starts the agent anyway |
| `systemd-run` or the user manager not available | client runs the command as a plain child process; the hub tracks it through the client's connection only, so a hub restart forgets it |
| `systemd-run` slow to create the scope | the hub waits up to 30 s for the scope before it counts the run as ended |
| Hub crashes during runs | runs keep going (they belong to systemd, not the hub); state rebuilt on restart |
| Hub restarts while a client is queued | client reconnects (up to 5 s each time, at most three times) and queues again, at the back of its class; after that it fails open |
| Client killed while queued | hub sees the socket close and drops the request |
| Client gone between grant and start, or hub restarted then | the run is adopted as soon as its scope runs; the grant is dropped after 15 s if it never does |
| Client killed while running | the scope lives on until the command ends; the hub still accounts it |
| Agent session crashes | pidfd closes; queued requests dropped; running runs finish |
| PSI triggers unavailable (kernel < 6.5, or the file refuses them) | the 2-second sampling of the pressure files, which runs anyway, is the only source |
| History file broken or locked | estimates come from the kind and the default; the hub logs a warning |
| Config error | hub exits with 78 and names the file, line and key; systemd does not restart it; clients fail open |

## 9. Security

- Single user, no root, no network. The socket is mode 0600 in `$XDG_RUNTIME_DIR`, and the hub checks
  `SO_PEERCRED` so only the same uid can talk to it.
- The hub never executes commands. It only grants, observes, and stops units named `agenthub-run-*` that a
  client of the same user registered.
- `agenthub mcp` executes the commands an agent passes to its `run` tool, as the user, outside the agent's own
  sandbox and approval rules; the tool is marked destructive so clients that ask before such calls ask, and the
  server is meant only for agents that may run shell commands anyway ([other-agents.md](other-agents.md#security)).
  `agenthub wrap` changes no approvals: the agent asks for the same commands as without it.
- The history for estimates stores only the fingerprint (program, subcommand, project root), never the whole
  command line, so secrets passed as arguments do not reach the disk; the file is mode 0600 anyway. Command
  lines of live runs are kept in the hub's memory and its runtime directory (`$XDG_RUNTIME_DIR`, memory-backed);
  the hub's journal names a shed run by its fingerprint.

## 10. Configuration

`$XDG_CONFIG_HOME/agenthub/config.toml`:

```toml
[machine]
desktop_reserve = "4G"      # never handed to agent work (default 15 %)
agent_budget = "auto"       # RAM minus desktop_reserve
default_estimate = "1G"     # a run with no estimate and no matching kind
idle_exit = "10m"           # the hub exits when idle; the socket starts it again
aging_step = "5m"           # a waiting run rises one class per this much waiting, up to p1
system_pressure = true      # also react to /proc/pressure/memory, not only to the agent slice

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

Sizes take `K/M/G/T` (binary) or a percentage of RAM; durations take `ms/s/m/h`. A pool has 1 to 1000 units, and a
kind may name only pools declared under `[pools]`. `$AGENTHUB_CONFIG` points to another file; `$AGENTHUB_PREFIX`
renames the units, the socket and the state directory (the tests use `agenthubtest`). The slice limits are written
into the unit files at `agenthub install`, so a changed budget needs `agenthub install` again.

## 11. Language and size

Python 3.11+ with the standard library only (`asyncio`, `socket`, `sqlite3`, `tomllib`), talking to systemd
through `systemd-run`/`systemctl` and to the kernel through cgroupfs and `/proc/pressure`. The target is a hub
small enough to read in one sitting. A port to a compiled language stays possible because the protocol is
plain JSON lines.

## 12. Testing

Three layers: `tests/test_config.py`, `tests/test_scheduler.py` and `tests/test_pressure.py` (pure logic:
admission, aging, pools, victim choice, fingerprints, the pressure governor with a fake clock),
`tests/test_hub.py` (the hub over its socket with fake memory readings, a fake cgroup tree and fake pressure
files), and `tests/test_integration.py` (the real user manager, only with `AGENTHUB_IT=1`: prefix
`agenthubtest`, a 600 MiB budget, `sleep`, CPU burners pinned to one core, and a memory storm of three hogs
with swap turned off for the test slice, which must shed exactly the newest `p3` run while a probe standing in
for the desktop keeps its 50 ms rhythm). `tests/test_claude.py`, `tests/test_wrap.py` and `tests/test_mcp.py`
run the integrations as their agents meet them — hook JSON, shims and wrapped processes, MCP over pipes —
against such a hub; the integration tests add two wrapped agents sharing the queue. The plan for all milestones:

- **Scheduler core as pure functions:** admission, ordering, aging, backfill and victim choice are tested with a
  fake clock and fake memory readings.
- **Integration on a real user manager:** synthetic memory hogs in a test slice with a small budget; checks
  that admission holds, PSI shedding fires, the victim is the right one, weights apply.
- **Edge matrix:** hub down, hub restart mid-run, client killed while queued and while running, session crash,
  config error, kernel without PSI triggers, pool exhausted, estimate far too low, run that never ends,
  concurrent requests racing for the last free memory, two sessions with the same class.
