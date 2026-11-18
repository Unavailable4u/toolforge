# P03: Registry: lifecycle, blobs, search

Depends on: P02.

## Goal

One module that owns every fact about tools: versions, their status, gate results, frozen blobs, approvals, call stats,
and search. Pure standard library (`sqlite3`).

## Scope

`src/toolforge/registry/{db.py,store.py,lifecycle.py,search.py}`, `tests/test_registry_*.py`, CLI: `toolforge registry list|show`.

## Data model

Tables (create with an ordered, idempotent migration list; keep a `schema_version` table):

* `tools(name PRIMARY KEY, active_version TEXT NULL, created_at)`
* `versions(id INTEGER PK, name, version, status, code_sha, blob_sha NULL, manifest_json, created_by, parent_version NULL, created_at, updated_at, UNIQUE(name, version))`
* `gate_results(id PK, version_id, gate, status, detail_json, started_at, finished_at)` (status `passed|failed|skipped`)
* `calls(id PK, version_id, ok INTEGER, duration_ms, error_class NULL, session_id NULL, ts)`
* `approvals(id PK, version_id, requested_json, status, decided_by NULL, decided_at NULL, created_at)`
* `tool_search` FTS5 virtual table over `name, description, when_to_use, examples_text`, rows only for `active` versions.

Open the database in WAL mode with a busy timeout; serialise writes with one process-level lock. Provide a context
manager for transactions.

## Statuses and transitions (`lifecycle.py`)

Statuses: `submitted, verifying, rejected, verified_basic, verified, pending_approval, active, deprecated, quarantined`.
Allowed transitions (anything else raises `IllegalTransition`):

```
submitted -> verifying
verifying -> rejected | verified_basic | verified
verified_basic -> active          (operator `promote` only, requires a reason; refused for network or secrets)
verified_basic -> pending_approval   (manifest asks for network or secrets while G5 was skipped; routed by P09)
verified -> pending_approval | active
pending_approval -> active | rejected
active -> deprecated | quarantined
quarantined -> active             (operator `release`, requires a reason; P08)
deprecated -> active              (operator `rollback`, requires a reason; the version must have been active before; P12)
```

`transition(version_id, to, *, actor, reason=None)` writes the new status, `updated_at`, and returns an event dict
`{type: "forge.<verb>", ...}` for the caller to append to the ledger (the registry does not write ledgers). Promoting to
`active` sets the tool's `active_version`, moves the previous active version to `deprecated`, and updates `tool_search`,
all in one transaction. Rollback (P12) is the mirror image in one transaction: the current active version (if any) becomes
`deprecated` and the chosen `deprecated` version becomes `active`. Moving an active version to `quarantined` clears
`active_version`; the tool then has no active version until the operator releases it (P08) or rolls back to an earlier one (P12).

## Blob store (`store.py`)

Content-addressed files `blobs/<sha256>.tar.gz`. Writes go to a temp file then `os.replace`. Reads re-hash and raise
`RegistryError` on mismatch. API: `put(bytes) -> sha`, `get(sha) -> bytes`, `exists(sha)`, `size(sha)`.

## Search (`search.py`)

`search(query, limit=5) -> list[Hit]` over active tools using FTS5 `bm25`, with the query sanitised to plain tokens
(strip FTS operators, quote tokens, join with OR, cap at 12 tokens). If FTS5 is unavailable, fall back to a token-overlap
score computed in Python and log a warning once. `Hit` carries name, version, description, score, status, uses, success_rate.
Ranking refinements are P12.

## Stats

`record_call(version_id, ok, duration_ms, error_class, session_id)`; `stats(name_or_version)` returns uses and
success rate over the last 50 calls (`None` rate when fewer than 5 calls, never 0).

## Tests

Migration idempotence; illegal transitions raise (and `verified_basic -> pending_approval` and `deprecated -> active` are allowed); promotion deprecates the previous active version atomically (simulate a
failure mid-transaction and check nothing changed); blob corruption detected; search finds a tool by words from its
description or examples and ignores deprecated ones; FTS query injection strings (`" OR 1`, `*`, `NEAR(`) do not raise;
stats with few calls report `None`; concurrent writers (threads) do not corrupt state.

## Acceptance

`pytest tests/test_registry_*.py -q`; `toolforge registry list` on an empty home prints an empty table without error.

## Pitfalls

Never store tool source in `versions` as text (the blob is the source of truth, the manifest JSON is indexed
metadata). Never delete rows; statuses record history. All timestamps are UTC ISO strings.
