# The registry

The registry is the only place that reads or writes tool state: versions, their status, gate results, frozen packages,
approvals, call statistics and search. Code lives in `src/toolforge/registry/`. It is pure standard library (`sqlite3`) and never
runs tool code.

```
$TOOLFORGE_HOME/
  registry.sqlite3          versions, status, gate results, calls, approvals, search index (SQLite, WAL mode)
  blobs/<sha256>.tar.gz     frozen tool packages, named by the SHA-256 of their bytes
```

Look at it with `toolforge registry list` and `toolforge registry show <name>[@<version>]` (add `--json` for scripts). Both are
read-only. A missing measurement is printed as "not measured", never as 0.

## What is stored

| Table | Holds |
| --- | --- |
| `tools` | One row per tool name and the version that is `active` now (or none). |
| `versions` | One row per version: status, `code_sha` (hash of the package archive), `blob_sha` (the frozen package, once built), the manifest as JSON, who created it, its parent version, timestamps. The tool source is **not** in this table; the blob is the source of truth. |
| `gate_results` | One row per gate run: gate, `passed` / `failed` / `skipped`, detail (plain JSON, capped at 64 KB), start and finish time. |
| `calls` | One row per finished call: ok, duration, error class, session id, time. |
| `approvals` | One row per approval request. P03 stores them; the approval flow is patch P09. |
| `tool_search` | The full-text index over name, description, `when_to_use` and example text. It has rows only for `active` versions. |
| `schema_version` | The migrations applied. |

Rules that hold everywhere: versions are immutable, rows are never deleted, every timestamp is a UTC ISO string ending in `Z`.
Migrations are an ordered list in `registry/db.py` that is applied once and in order; a database written by a newer ToolForge is
refused rather than guessed at.

## The lifecycle

A tool name has at most one `active` version. `Registry.transition(version_id, to, actor=..., reason=...)` is the only way
a status changes; any move not in this table raises `IllegalTransition` and changes nothing.

<!-- transitions:begin -->
| From | To | Event | Rule |
| --- | --- | --- | --- |
| `submitted` | `verifying` | `forge.verifying` | The pipeline starts. |
| `verifying` | `rejected` | `forge.rejected` | A gate failed. |
| `verifying` | `verified_basic` | `forge.verified` | Every gate that could run passed, but one was skipped. |
| `verifying` | `verified` | `forge.verified` | Gates G1 to G6 passed. |
| `verified_basic` | `active` | `forge.promoted` | Operator `promote` only, with a reason. Refused when the manifest declares network hosts or secrets. |
| `verified_basic` | `pending_approval` | `forge.approval.requested` | The manifest asks for network or secrets and G5 was skipped (routed by P09). |
| `verified` | `pending_approval` | `forge.approval.requested` | The manifest asks for network or secrets. |
| `verified` | `active` | `forge.promoted` | Automatic promotion. Refused when the manifest declares network hosts or secrets. |
| `pending_approval` | `active` | `forge.approval.granted` | The operator approves. |
| `pending_approval` | `rejected` | `forge.approval.denied` | The operator denies, or the request times out. |
| `active` | `deprecated` | `forge.deprecated` | A newer version was promoted, or the operator retires it. |
| `active` | `quarantined` | `forge.quarantined` | A runtime violation or a failure rate over the threshold. |
| `quarantined` | `active` | `forge.released` | Operator `release`, with a reason (P08). |
| `deprecated` | `active` | `forge.rolled_back` | Operator `rollback`, with a reason (P12). |
<!-- transitions:end -->

Beyond the table:

* **Operator-only moves** (`actor="operator"`, nothing else is accepted): promoting a `verified_basic` version, approving, releasing
  a quarantined version and rolling back. Promote, release and rollback also need a non-empty reason.
* **Network or secrets need approval.** A version whose manifest declares network hosts or secrets can never go to `active`
  from `verified` or `verified_basic`; it goes through `pending_approval`.
* **Promotion is atomic.** Making a version `active` also deprecates the previous active version, updates `tools.active_version`
  and the search index, all in one transaction. If any step fails, nothing changes. Rollback uses the same code, so it is the
  exact mirror image. Quarantining the active version leaves the tool with no active version until the operator releases it or
  rolls back to an earlier one.
* **Rollback targets are safe by construction.** `deprecated` can only be reached from `active`, so every deprecated version
  passed every gate and approval once.
* **The registry writes no ledger.** `transition` returns the event (`type`, `tool`, `version`, `version_id`, `from`, `to`,
  `actor`, `code_sha`, `ts`, and `reason` when given) for the caller to append. A promotion's event carries a `related` list:
  the events for the versions it deprecated, which belong in those versions' own ledgers.

## Blobs

`registry.blobs` is a content-addressed store: `put(bytes) -> sha`, `get(sha)`, `exists(sha)`, `size(sha)`. Writes go to a temporary
file and are renamed into place, so a reader never sees half a blob. `get` hashes what it reads and raises `blob_corrupt` when it
no longer matches its address, so a damaged blob is never handed to a sandbox. An address is exactly 64 lower-case hex characters;
anything else is refused before any path is built. `attach_blob` records a blob on a version once, and only while the version is
`submitted` or `verifying`.

## Search

`registry.search(query, limit=5)` returns the best active tools, each with name, version, description, score, status, uses and
success rate. The query is reduced to at most 12 plain words and every word is quoted before it reaches SQLite, so a query cannot
carry FTS syntax. Words are stemmed and `word_count` is found by "word". Ranking is bm25 with fixed weights that favour the name over
the description over the examples; smarter ranking (success rate, recency, permissions) is patch P12.

## Statistics

`registry.record_call(...)` and `registry.stats(version_id | "name@version" | "name")`. `uses` counts every call; `success_rate`
covers the last 50 calls and is `None` ("not measured") until there are at least 5.

## Limits of this patch

* **History lives in the ledger, not here.** The registry keeps the current status and `updated_at`, not a log of past moves. The
  event that `transition` returns is the history; if a caller fails to append it, that move leaves no trace but the new status.
* **The registry cannot tell who "operator" is.** It accepts the string. The protection is meant to be that no MCP tool
  reaches `transition`, `decide_approval` or `attach_blob`. No MCP server exists yet; patch P09 is specified to add an `ast` test
  that enforces it, and until then this is a rule, not a check.
* **Search without FTS5 is cruder.** If this SQLite has no FTS5 when the database is created, search falls back to counting the
  query words found in each tool (scores between 0 and 1, logged once). Scores from the two back ends are not comparable. A database
  created without FTS5 keeps using the fallback even if SQLite is upgraded later.
* **Blobs are not inspected, capped or garbage collected.** The store trusts the caller for what the bytes are and how big they
  may be (the build gate caps them in P06), and nothing deletes a blob.
* **Local disk only.** SQLite's WAL mode does not work over network file systems. Writers wait at most 5 seconds for each other
  (across processes too) and then fail with `registry_busy`.
