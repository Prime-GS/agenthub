# Prior art

Research done in November 2026. Star counts and dates are as of that time.

## The problem is common

- [anthropics/claude-code#101193](https://github.com/anthropics/claude-code/issues/101193) — feature request:
  coordinate heavy local work across concurrent sessions. The reporter's 11-core laptop reached load ~726 with
  swap full; several commenters built their own semaphores. Open.
- [#63318](https://github.com/anthropics/claude-code/issues/63318) — about ten parallel sessions, ~30 GB memory
  pressure.
- [#45880](https://github.com/anthropics/claude-code/issues/45880) — each session starts its own MCP server
  processes without limits; kernel panic.
- [#62193](https://github.com/anthropics/claude-code/issues/62193) — ~3,000 bash processes in 17 s, OOM, hard
  reboot.
- [#18011](https://github.com/anthropics/claude-code/issues/18011), [#22172](https://github.com/anthropics/claude-code/issues/22172) —
  memory and CPU problems with several sessions at once.

Agent tools limit concurrency only inside one session (subagent caps). Nothing shares a budget across sessions.

## Tools

| Tool | What it does | Missing for this goal |
|---|---|---|
| [block/agent-task-queue](https://github.com/block/agent-task-queue) (Apache-2.0) | MCP server; FIFO queues with capacities, hierarchical queues, cleans up orphaned builds, output to log files | No priorities; no memory or CPU awareness; agents must call the MCP tool |
| [staterijndael/agent-slots](https://github.com/staterijndael/agent-slots) (MIT) | Claude Code plugin: SessionStart + PreToolUse hook refusing heavy commands without a slot; pools lowered by free memory and load; `status` explains the queue | FIFO only; no daemon — every waiter polls a shared JSON file under `flock`; macOS-first, very new |
| [AW-HTK/clearway](https://github.com/AW-HTK/clearway) (MIT) | MCP queue measuring RAM and CPU, hook refuses heavy commands without a slot | Windows/WSL only; FIFO |
| [Nukesor/pueue](https://github.com/Nukesor/pueue) (Apache-2.0) | Mature task queue daemon: groups with parallel limits, task priority | Not memory-aware, not agent-aware; output is asynchronous |
| [eunomia-bpf/agentcgroup](https://github.com/eunomia-bpf/agentcgroup) (GPL-2.0), [paper](https://arxiv.org/abs/2602.09345) | Research: per-tool-call cgroups, sched_ext CPU scheduler, in-kernel memory priority | Needs root, sched_ext and out-of-tree `memcg_bpf_ops` kernel patches |
| [pop-os/system76-scheduler](https://github.com/pop-os/system76-scheduler), ananicy-cpp, [uresourced](https://www.fedoraproject.org/wiki/Changes/Reserve_resources_for_active_user_WS) | Root daemons: prioritise the desktop and the focused app, push compilers down | No sessions, no queue, no memory admission |
| [earlyoom](https://github.com/rfjakob/earlyoom), [nohang](https://github.com/hakavlad/nohang), [oomd](https://github.com/facebookincubator/oomd), systemd-oomd | Kill something when memory is nearly gone | Last resort, not scheduling |

## What agenthub takes from them

- From agent-slots and Clearway: route heavy commands with a hook so agents need no instructions; explain why a
  run waits.
- From agent-task-queue: queues per resource kind (pools); keep build output out of the agent's context.
- From uresourced and system76-scheduler: let cgroup weights and memory protection do the CPU part.
- From oomd: act on PSI, not on free-memory numbers.
