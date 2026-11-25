# Stateful sessions (P13)

Most tools are one call in one fresh sandbox. A few capabilities are not: a game in progress, a document being edited, a database being
explored. A tool with `"kind": "session"` runs inside a **session**: one sandbox that stays alive across calls, with snapshots, rewind,
time limits and an audit, all still bounded the way a single call is.

## For an agent

* `forge_run(name, args)` on a session tool, **without** `session_id`, opens a session and returns `session_id`, `step` and
  `session_expires_in_s`. Pass the same `session_id` on later calls to continue.
* `forge_session_list` shows your session (pass its id) or every open one by public name. `forge_session_rewind(session_id, step)` goes back to
  the state after an earlier call; step 0 is the state before the first call. `forge_session_close(session_id)` ends the session and
  returns the verdict of its Blast Radius report.
* A failed call is not a step: the state is the one before it. A call whose sandbox died or timed out is recovered to the last step
  automatically; if that fails the session is closed as `lost`.
* A session id is `s-<8 hex>.<32 hex>`. The part before the dot is public (lists, logs); the part after it is the only proof that the
  session is yours, so keep the whole id to yourself. An unknown id and a wrong secret get the same error (`bad_session`).
* A session is bound to the version it opened with: it keeps working if that version is deprecated, and is closed if it is quarantined. It only
  runs the tool that opened it (`session_other_tool`).

## For a tool author

```python
def run(args: dict, state: dict) -> object:
    state["n"] = state.get("n", 0) + args.get("by", 1)
    return {"n": state["n"]}
```

The harness loads `state` from `/workspace/.tf/state.json` before the call and saves it after a **successful** call. State must be JSON
and at most 256 KB (`OutputTooLarge` otherwise; nothing is saved). Keep nothing else between calls: the package, harness and SDK are put
back before every call, and gate G3 re-runs the first example in a new sandbox to catch a tool whose answer depends on anything
outside `state`. A game with an engine that cannot be stored keeps a seed plus the list of actions and replays them.

## Limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `session_ttl_s` | 900 | closed after this many seconds without a call |
| `session_max_s` | 3600 | closed this long after it opened, whatever happens |
| `max_sessions` | 4 | open at once (counted in the registry); the next open waits 5 s and then fails with `Busy` |

The last 5 snapshots are kept in memory, each at most 8 MiB; a call whose snapshot would be larger still succeeds but cannot be rewound to.
A reaper thread (every 5 s, started on first use) closes expired sessions.

## What is audited

* Each sandbox a session uses writes its own box ledger, `ledgers/<tool@version>/<public>-<n>.jsonl`, with the same call events a single
  call writes. The decoy tripwire runs after **every** call; the real-secret audit runs when a box is closed (end of session, rewind or
  recovery), so an answer delivered earlier was already out by then.
* The session's own events (`forge.session.opened`, `step`, `rewound`, `recovered`, `closed`) are in `ledgers/<tool@version>/sessions/<public>.jsonl`.
  They hold hashes and sizes only, never arguments or results.
* Closing stores `sessions/<public>-blast-radius.json`: the Blast Radius report of the session's sandboxes. Its verdict says only what those runs showed.

## For the operator

```
toolforge sessions list [--all]        # open sessions (--all: also ended ones, with the reason)
toolforge sessions close <session>     # ask the owning server to close it (audit included); waits up to 20 s
toolforge sessions close --all
toolforge doctor --clean               # leaves containers of live sessions alone; removes orphaned ones and marks them lost
```

The registry's `sessions` table is what other processes see: the owner process refreshes a heartbeat every few seconds, and an open
session whose heartbeat is older than 45 s is orphaned (its server died). `sessions close` on an orphan removes its container directly,
with no audit, and says so. `toolforge run` on a session tool runs one call and closes the session; longer sessions belong to the MCP server.
