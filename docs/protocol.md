# Protocol

Clients talk to the hub over a Unix stream socket at `$XDG_RUNTIME_DIR/agenthub.sock`. Messages are JSON
objects, one per line (UTF-8, `\n`-terminated). Every request carries `"v": 1` and an `"op"`; every answer
carries `"ok"` and, on failure, `"error"` with a machine-readable `code` and a human `message`. The hub accepts
connections only from its own uid (`SO_PEERCRED`). With `$AGENTHUB_PREFIX` set the socket is `<prefix>.sock`.

Sizes in answers are bytes; times are Unix timestamps; durations are seconds. Requests accept sizes as bytes or
strings such as `"2G"`, and durations as seconds or strings such as `"10m"`.

A connection may carry several requests. A `request` for a run keeps its connection open until the run ends:
the open connection is how the hub knows the client is still waiting or still running. Closing it drops a
queued request.

## Sessions

```json
{"v":1,"op":"session.start","session":"a1f3…","agent":"claude-code","pid":48211,"cwd":"/home/u/work/api","title":"orders refactor"}
{"ok":true,"session":"a1f3…","class":"p1","source":"project rule ~/work/**"}

{"v":1,"op":"session.end","session":"a1f3…"}
{"ok":true}

{"v":1,"op":"session.prio","session":"a1f3…","class":"p0"}
{"ok":true,"class":"p0","source":"override"}
```

A session id is up to 128 letters, digits and `._:@-`, starting with a letter or digit (it names a file in the
hub's state directory); anything else is a `bad_request`. `pid` is optional; when given, the hub watches that process through a pidfd and ends the session when it exits,
cancelling its queued runs. A `pid` that does not exist is a `bad_request`. Registering an existing session
again keeps its override.

## Runs

```json
{"v":1,"op":"run.request","session":"a1f3…","agent_id":null,"cmd":"npm run build","cwd":"/home/u/work/api",
 "kind":null,"pools":null,"estimate":null,"wait":"600s"}
```

`kind`, `pools`, `estimate`, `hold` and `notify` are optional. `kind` names a kind from the config instead of
matching the command line; `pools` (a list of pool names) replaces the kind's pools; without `estimate` the hub
takes the learned one, then the kind's, then the default ([design §4.2](design.md#42-estimates)). An unknown kind
or pool is answered with `unknown_kind` or `unknown_pool`, and the client exits with 2 without running the command.
`wait` is how long the client is prepared to wait in the queue; without it the client waits as long as it takes.
`hold` (a duration above 0 and at most 600 s) is how long a grant waits for a client after this connection closes,
instead of 15 s: a hook that waited for the grant leaves, and the command it rewrote claims the grant later.
`notify: true` asks for a news line to the session when the room this run needed appears after its `wait` ran out.

Answers on the same connection, in order:

```json
{"ok":true,"state":"queued","run":43,"position":2,"reason":"needs 3.1 GiB, 1.2 GiB above reserve"}
{"ok":true,"state":"queued","run":43,"position":1,"reason":"behind run 41 (p1, needs 2.0 GiB)"}
{"ok":true,"state":"queued","run":43,"position":1,"reason":"pool browser 1/1 held by run 41"}
{"ok":true,"state":"queued","run":43,"position":1,"reason":"memory pressure — admissions paused"}
{"ok":true,"state":"granted","run":43,"unit":"agenthub-run-43.scope","slice":"agenthub-p2.slice","memory_high":4939212390}
```

The client then starts the scope and confirms:

```json
{"v":1,"op":"run.started","run":43,"unit":"agenthub-run-43.scope","pid":50112}
```

A grant whose client left on purpose (`hold`) is taken over on a new connection:

```json
{"v":1,"op":"run.claim","run":43,"session":"a1f3…"}
{"ok":true,"state":"granted","run":43,"unit":"agenthub-run-43.scope","slice":"agenthub-p2.slice","memory_high":4939212390}
```

`session` is required. The answer is the grant itself, and the exchange goes on as after a `run.request`. A
run that is no longer granted, belongs to another session, or already has a client is answered with
`grant_gone`; the client then requests again.

`unit` is `null` when the client could not make a scope (no `systemd-run` or no user manager): the hub then
tracks the run only through this connection and ends it when the connection closes. With a scope, the hub
waits up to 30 s for it to appear before it treats the run as ended.

While the run lives the hub may send:

```json
{"ok":true,"state":"stopping","run":43,"reason":"shed: memory full-pressure 74 ms/2 s (agents); lowest class, newest run"}
```

and the client prints the reason and exits with 75 once the scope is gone. A shed run's reason starts with
`shed: ` and names what fired: `memory full-pressure N ms/2 s (agents|system)` from sampling,
`memory full-pressure ≥ 50 ms/2 s (agents|system, trigger)` from a PSI trigger, or `MemAvailable X below half
the desktop reserve`. A `queued` update is sent only when
the position or the reason changes. When the command ends the client sends `run.ended` with the exit code; the
hub's answer closes the exchange:

```json
{"v":1,"op":"run.ended","run":43,"exit":0}
{"ok":true,"run":43,"peak":2576980378,"duration":185.0,"stopped":null}
```

`peak` is the highest `memory.peak` the hub saw (it samples every 2 s); `null` when the scope was gone before
the first sample. The hub also learns the end on its own from the
scope's `cgroup.events`, so a client that dies while its command runs does not leave the run counted forever.
A connection that closes while its run is queued, or granted but not started, cancels that run.

Timeouts: if `wait` passes while queued the hub answers `{"ok":false,"error":{"code":"wait_expired",…}}` whose
message carries the last reason; the client exits with 75. If the run is cancelled while queued (its session
ended, or `run.stop`), the answer is `cancelled`, also exit 75.

## Status

```json
{"v":1,"op":"status"}
{"ok":true,"machine":{…},"sessions":[…],"runs":[…],"queue":[…],"recent":[…]}

{"v":1,"op":"why","run":43}
{"ok":true,"run":43,"state":"queued","class":"p3","effective_class":"p2","estimate":3328599654,
 "estimate_source":"history","reason":"needs 3.1 GiB, 1.2 GiB above reserve",
 "free":5153960755,"growth":3865470566,"headroom":1288490189,"position":1,"ahead":[],"fits":false,
 "checks":[{"rule":"admissions","pass":true,"reason":null},
           {"rule":"memory","needs":3328599654,"headroom":1288490189,"pass":false},
           {"rule":"pool","pool":"browser","used":0,"size":2,"pass":true}]}
```

`effective_class` is the class after aging; `estimate_source` is `explicit`, `history`, `kind <name>` or
`default`. `checks` (queued runs only) lists every admission rule with its result.

`machine` holds `total`, `available`, `reserve`, `budget`, `agent_current`, `free`, `growth`, `headroom`, the
config source, `pressure` (`memory`, `agents` and `cpu`, each `{"some": {"avg10", "avg60", "avg300", "total"},
"full": {…}}` from `/proc/pressure/*` and the agent slice), `admissions` (`state` `running` or `paused`, `reason`,
`by_hand`, `pressure`) and `pools` (`{"browser": {"size": 2, "held_by": [41, 42]}}`). Sessions carry `id`,
`agent`, `pid`, `cwd`, `title`, `class`, `source`, `override`, `started`. Runs carry `id`, `session`, `agent_id`,
`cmd`, `cwd`, `kind`, `class`, `estimate`, `requested`, `wait`, `state`, `reason`, `granted`, `started`, `ended`,
`unit`, `pid`, `exit`, `peak`, `stopping`, `pools`, `fp` (the history fingerprint), `estimate_source`, `shed`,
`cpu_usec`. `runs` are the granted and running runs, with `current` memory and the live `cpu_usec`; `queue` adds
`position` and `effective_class`; `recent` holds the last ten finished runs.

## Control

```json
{"v":1,"op":"run.stop","run":41,"reason":"user"}
{"ok":true,"run":41,"state":"stopping"}

{"v":1,"op":"admissions","state":"paused","reason":"desktop demo"}
{"ok":true,"state":"paused","reason":"admissions paused by hand (desktop demo)","by_hand":"desktop demo","pressure":null}
{"v":1,"op":"admissions","state":"running"}
{"v":1,"op":"admissions"}
```

`run.stop` is allowed for any run of the same user; it is how a person or an agent frees room by hand. A
running run is told `stopping` and its scope is stopped; a queued one is cancelled.

`admissions` with `state` `paused` stops granting new runs (running ones are not touched) until `running`;
without `state` it only reports. The pause by hand is kept across hub restarts. `pressure` is the pause the hub
set itself under memory pressure: it lifts after 30 calm seconds and `running` does not lift it. Any other
`state` is a `bad_request`.

## Error codes

| Code | Meaning |
|---|---|
| `bad_request` | malformed message or unknown op |
| `unknown_session` | session not registered (the client registers and retries once) |
| `unknown_run` | no such run |
| `unknown_kind` | `kind` names no kind in the config (client exit 2) |
| `unknown_pool` | a pool in `pools` is not declared in the config (client exit 2) |
| `grant_gone` | `run.claim` found no grant to take over (the client requests again) |
| `wait_expired` | waited longer than the request allowed (client exit 75) |
| `cancelled` | a queued run was dropped: its session ended or it was stopped (client exit 75) |
| `refused` | the run can never fit: estimate above the whole agent budget (client exit 69) |
| `version` | protocol version not supported |
| `internal` | the hub failed on this request (logged in its journal); the client fails open |

On any other error to `run.request` (`bad_request`, `internal`, `version`, a code it does not know) the client
runs the command directly and prints the hub's answer.
