# P13: Stateful sessions

Depends on: P05, P08.

## Goal

Some capabilities are inherently stateful: a game in progress, a document being edited, a database being explored. Add
sessions: a sandbox that stays alive across calls, with snapshots and rewind, still bounded and audited.

## Scope

`src/toolforge/core/sessions.py`, runner changes, MCP additions (a `session_id` parameter on `forge_run`, plus the new tools
`forge_session_close`, `forge_session_rewind`, `forge_session_list`), registry table for session metadata, CLI
`toolforge sessions list|close`, tests.

`META_TOOLS` (P07) is the only tool list: update the `forge_run` entry and append the three new tools in this patch, and change
the README tool table, `docs/clients.md` and the exact-tool-list test with them. Rewind and close act only on the caller's own
session; they are not registry actions and do not break the rule that no MCP tool approves, promotes, releases or rolls back a tool.
New settings, each with a documented default and an `.env.example` line: `session_ttl_s` (900), `session_max_s` (3600),
`max_sessions` (4).

## Model

A tool with `"kind": "session"` has signature `run(args: dict, state: dict) -> object`. The harness loads `state` from
`/workspace/.tf/state.json` before the call and saves it after, when the tool mutated it. State must be JSON. (Games with
unpicklable engines store a seed plus the action list and replay it; the recipe in P14 shows the pattern.)

`forge_run(name, args)` on a session tool without `session_id` opens a session and returns its `session_id`; later calls pass
it. A call that includes an unknown, expired, closed or other-tool `session_id` is an error.

## Lifecycle

* The session sandbox is created at open (image, limits, network as the manifest says, decoys on), the package thawed once.
* Each call runs the harness as a new process in that sandbox. After each call the runner takes a snapshot
  (`freeze`), keeps the last 5 in memory, and records a ledger event `forge.session.step`.
* Idle TTL (`session_ttl_s`, default 900) and max lifetime (`session_max_s`, default 3600); a reaper thread closes expired
  sessions. At most `max_sessions` (default 4) open at once; the next open call waits briefly then fails with a clear message.
* `forge_session_rewind(session_id, step)` restores snapshot `step` into a fresh sandbox (MiniLocker's rewind does the same:
  new sandbox, restore, verify) and records a ledger event; the history before the rewind stays in the ledger.
* Closing a session runs the audit and produces its Blast Radius report. Sessions survive deprecation of their tool version
  but not quarantine: quarantine closes them.
* A session is bound to the tool version it opened with. Sessions are never shared between callers.
* Runtime decoys and the tripwire apply per call exactly as in P08.

## Verification of session tools

G3 and G4 run the tool in a session sandbox for a short scripted sequence: the manifest examples, in order, as one session,
then a fresh session to confirm state does not leak between sessions. G6 audits the whole sequence.

## Tests

The exact `META_TOOLS` list includes the new tools and the `session_id` parameter; a caller cannot use another caller's session id.
`FakeRunner`: state load/save protocol, TTL expiry with an injected clock, max sessions, rewind chooses the right snapshot,
closing audits once, quarantine closes sessions. Docker: a counter tool incremented three times returns 1, 2, 3; a new session
starts at 1; rewind to step 1 then increment returns 2; TTL expiry removes the container.

## Acceptance

`pytest -q`; with Docker the counter test.

## Pitfalls

Leaked containers are bugs: the reaper and `toolforge doctor --clean` must remove them, including after a crash. Do not let
state files grow without bound (cap `state.json` at 256 KB and fail the call with a clear error).
