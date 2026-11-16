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

`kind`, `pools` and `estimate` are optional; the hub fills them from its rules and history (M1 reads only
`estimate`; `kind` and `pools` are reserved for M2). `wait` is how long the client is prepared to wait in the
queue; without it the client waits as long as it takes.

Answers on the same connection, in order:

```json
{"ok":true,"state":"queued","run":43,"position":2,"reason":"needs 3.1 GiB, 1.2 GiB above reserve"}
{"ok":true,"state":"queued","run":43,"position":1,"reason":"behind run 41 (p1, needs 2.0 GiB)"}
{"ok":true,"state":"granted","run":43,"unit":"agenthub-run-43.scope","slice":"agenthub-p2.slice","memory_high":4939212390}
```

The client then starts the scope and confirms:

```json
{"v":1,"op":"run.started","run":43,"unit":"agenthub-run-43.scope","pid":50112}
```

`unit` is `null` when the client could not make a scope (no `systemd-run` or no user manager): the hub then
tracks the run only through this connection and ends it when the connection closes. With a scope, the hub
waits up to 30 s for it to appear before it treats the run as ended.

While the run lives the hub may send:

```json
{"ok":true,"state":"stopping","run":43,"reason":"shed: memory full-pressure 74 ms/2 s; lowest class, newest run"}
```

and the client prints the reason and exits with 75 once the scope is gone. A `queued` update is sent only when
the position or the reason changes. When the command ends the client sends `run.ended` with the exit code; the
hub's answer closes the exchange:

```json
{"v":1,"op":"run.ended","run":43,"exit":0}
{"ok":true,"run":43,"peak":2576980378,"duration":185.0,"stopped":null}
```

`peak` is `null` when systemd has already removed the scope. The hub also learns the end on its own from the
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
{"ok":true,"run":43,"state":"queued","class":"p2","estimate":3328599654,"reason":"needs 3.1 GiB, 1.2 GiB above reserve",
 "free":5153960755,"growth":3865470566,"headroom":1288490189,"position":1,"ahead":[],"fits":false}
```

`machine` holds `total`, `available`, `reserve`, `budget`, `agent_current`, `free`, `growth`, `headroom`,
`pressure` (from `/proc/pressure/memory`) and the config source. Sessions carry `id`, `agent`, `pid`, `cwd`,
`title`, `class`, `source`, `override`, `started`. Runs carry `id`, `session`, `agent_id`, `cmd`, `cwd`, `kind`,
`class`, `estimate`, `requested`, `wait`, `state`, `reason`, `granted`, `started`, `ended`, `unit`, `pid`,
`exit`, `peak`, `stopping`. `runs` are the granted and running runs, with `current` memory and `cpu_usec`
added; `queue` adds `position`; `recent` holds the last ten finished runs.

## Control

```json
{"v":1,"op":"run.stop","run":41,"reason":"user"}
{"ok":true,"run":41,"state":"stopping"}
{"v":1,"op":"admissions","state":"paused"}
```

`run.stop` is allowed for any run of the same user; it is how a person or an agent frees room by hand. A
running run is told `stopping` and its scope is stopped; a queued one is cancelled. `admissions` comes with M2.

## Error codes

| Code | Meaning |
|---|---|
| `bad_request` | malformed message or unknown op |
| `unknown_session` | session not registered (the client registers and retries once) |
| `unknown_run` | no such run |
| `wait_expired` | waited longer than the request allowed (client exit 75) |
| `cancelled` | a queued run was dropped: its session ended or it was stopped (client exit 75) |
| `refused` | the run can never fit: estimate above the whole agent budget (client exit 69) |
| `version` | protocol version not supported |
| `internal` | the hub failed on this request (logged in its journal); the client fails open |
