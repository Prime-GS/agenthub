# Protocol

Clients talk to the hub over a Unix stream socket at `$XDG_RUNTIME_DIR/agenthub.sock`. Messages are JSON
objects, one per line (UTF-8, `\n`-terminated). Every request carries `"v": 1` and an `"op"`; every answer
carries `"ok"` and, on failure, `"error"` with a machine-readable `code` and a human `message`.

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

## Runs

```json
{"v":1,"op":"run.request","session":"a1f3…","agent_id":null,"cmd":"npm run build","cwd":"/home/u/work/api",
 "kind":null,"pools":null,"estimate":null,"wait":"600s"}
```

`kind`, `pools` and `estimate` are optional; the hub fills them from its rules and history. `wait` is how long
the client is prepared to wait in the queue.

Answers on the same connection, in order:

```json
{"ok":true,"state":"queued","run":43,"position":2,"reason":"needs 3.1 GiB, 1.2 GiB above reserve"}
{"ok":true,"state":"queued","run":43,"position":1,"reason":"memory pressure — admissions paused"}
{"ok":true,"state":"granted","run":43,"unit":"agenthub-run-43.scope","slice":"agenthub-p2.slice","memory_high":"4.6G"}
```

The client then starts the scope and confirms:

```json
{"v":1,"op":"run.started","run":43,"unit":"agenthub-run-43.scope","pid":50112}
```

While the run lives the hub may send:

```json
{"ok":true,"state":"stopping","run":43,"reason":"shed: memory full-pressure 74 ms/2 s; lowest class, newest run"}
```

and the client prints the reason and exits with 75 once the scope is gone. When the command ends on its own the
client sends `run.ended` with the exit code; the hub's answer closes the exchange:

```json
{"v":1,"op":"run.ended","run":43,"exit":0}
{"ok":true,"peak":"2.4G","cpu_seconds":412.5,"duration":"3m05s"}
```

Timeouts: if `wait` passes while queued the hub answers `{"ok":false,"error":{"code":"wait_expired",…}}` with
the current position and reason; the client exits with 75.

## Status

```json
{"v":1,"op":"status"}
{"ok":true,"machine":{…},"sessions":[…],"runs":[…],"queue":[…]}

{"v":1,"op":"why","run":43}
{"ok":true,"run":43,"state":"queued","checks":[{"rule":"memory","needs":"3.1G","headroom":"1.2G","pass":false}, …]}
```

## Control

```json
{"v":1,"op":"run.stop","run":41,"reason":"user"}
{"v":1,"op":"admissions","state":"paused"}
```

`run.stop` is allowed for any run of the same user; it is how a person or an agent frees room by hand.

## Error codes

| Code | Meaning |
|---|---|
| `bad_request` | malformed message or unknown op |
| `unknown_session` | session not registered (the client registers and retries once) |
| `unknown_run` | no such run |
| `wait_expired` | waited longer than the request allowed |
| `refused` | the run can never fit (estimate above the whole agent budget) |
| `version` | protocol version not supported |
