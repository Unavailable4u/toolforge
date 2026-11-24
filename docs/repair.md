# Finding, calling and repairing tools (P12)

This page is for the operator who runs ToolForge and for the person who wants to know what an agent can and cannot do to a tool
that has started to fail. Everything here was added in patch P12.

## How `forge_find` ranks

`forge_find` returns the best **active** tools for a request, at most five. The registry finds candidates with SQLite's bm25 (name,
then description, then `when_to_use`, then the examples) and orders them with one formula, whose numbers all live in one constant
block, `RANKING` in `src/toolforge/registry/search.py`:

| Term | Weight | What it measures |
| --- | --- | --- |
| text | 1.0 | 60% bm25 (relative to the best candidate), 40% the share of your words that the name, description and `when_to_use` cover |
| examples | 0.20 | your words that also occur in the example *inputs* |
| reliability | 0.30 | the smoothed success rate minus 0.75: `(ok + 3) / (calls + 4)` over the last 50 calls, so a tool with no calls is neutral and one lucky call moves it by 0.05 |
| recency | 0.05 | 1 for a version that became active just now, halving every 60 days |
| permissions | 0.03 | a tie-break: fewer hosts and secrets rank higher |

A track record can only reorder tools that already match: a tool that shares none of your words is not a candidate however well it
has done. Ties are broken by name, so the order never depends on row order. Without SQLite's FTS5 the same formula runs on a plain
word-overlap relevance, so both back ends rank alike.

`confidence` is **not a quality score**. It says how much of what you asked the tool's own description covers:

* `high`: at least 75% of your words are covered *and* the smoothed success rate is at least 0.60;
* `medium`: at least 40% are covered, or the tool covers the request but its recent calls mostly fail;
* `low`: anything less.

Each result has a `why`, one line made only of numbers and fixed words (`covers 3 of 3 of your words; 9 of 12 recent calls ok; no
network or secrets`). When the best result is `low`, or nothing matched, the answer says to build a tool (`forge_build`) or submit
one. Changing a number in `RANKING` is a decision: `tests/test_search_ranking.py` pins what each one does.

## Call signatures

`forge_describe` includes `signature`: the call as a Python and a TypeScript stub, built from `input_schema` and `output_schema`
only (`src/toolforge/server/signature.py`). It is there so a model that writes code does not have to read a JSON Schema.

```python
def csv_summary(path: str, delimiter: str = ",") -> dict[str, Any]: ...
```
```ts
declare function csv_summary(args: { path: string; delimiter?: string }): Promise<{ rows: number }>;
```

A stub describes how to call a tool. It is not an importable module, it runs nothing, and tools run only through `forge_run`.
`status` is `generated` when every construct was translated, `partial` when something became `Any` / `unknown` (the first note names
the construct: `oneOf`, `$ref`, `patternProperties`, a schema in `additionalProperties`, `format`, a type list, nesting deeper than
four levels), and `not_available` when there is no usable `input_schema`.

The schema comes from a tool's author, so it is treated as hostile: a name becomes a parameter only if it is a plain identifier that is
not a Python or TypeScript keyword (otherwise the stub takes one `args` object), enum values and defaults appear only as short, escaped
string literals, no prose field ever appears, and each stub is at most 2000 characters. `tests/test_signature.py` has the hostile cases.

## The context budget

What the server sends a model is bounded (`src/toolforge/server/budget.py`, measured in characters of JSON; a token is *estimated* as
four characters, there is no tokenizer): `tools/list` does not change with the number of tools, a five-result `forge_find` fits
`find_chars`, and a `forge_describe` response fits `describe_chars` (a larger one is cut down in a fixed order and says
`truncated`). `tests/test_context_budget.py` fails when a response outgrows its budget, so a bigger response is a decision, recorded in
`docs/guide/DEVIATIONS.md`.

## When a tool fails

Every call that ends in `Crash`, `Timeout`, `SandboxCrashed`, `NoResult` or `PolicyViolation` is captured with its error class and a
**shape fingerprint**: the types and sizes of the arguments (a string's length, a number's kind and digit count, a list's length),
never values, and only the declared top-level keys by name. File names are never kept.

### Storing the failing input (off by default)

`TOOLFORGE_STORE_FAILED_INPUTS=true` also keeps the raw arguments of a failed call, so the next version of the tool is tested against
them (G3 runs each stored input and the version must return a result or raise `ToolError`). Know what that stores before you turn it
on:

* the arguments **as the caller sent them**, which may be personal data, in `registry.sqlite3`, **unencrypted**;
* only if they are under 8 KB, came with no files, and `secretscan` finds nothing secret-shaped in them (the scan is a heuristic, not
  a promise);
* at most the 20 newest inputs per tool; when a newer one arrives the oldest is blanked (its fingerprint row stays), and the same input
  twice is kept once;
* no command deletes them. To drop them all, delete the `input_json` values from the `regression_cases` table, or the registry file.

With the setting off, only the fingerprint is kept, and not even a hash of the input (a hash of a short input can be guessed).

### Automatic quarantine

If more than 40% of a version's last calls fail, and at least 10 of the last 20 calls count, it is quarantined (`forge.quarantined`,
actor `forge`, with the numbers as the reason). A handled `ToolError` is left out of that window altogether: it is the tool refusing
bad input, which is its job. A call rejected by schema validation never reaches the statistics. The window starts again whenever a
version changes status, so a released or rolled-back version is not pulled again for the failures that pulled it. Only measured
outcomes count: **a problem report never pulls a tool**.

### Problem reports

`forge_report_problem(name, version, description)` stores up to 500 characters for you to read (`toolforge problems [name[@version]]`).
It is cleaned, scanned for secrets (a report with one is refused), limited to 5 an hour per reporter and 20 per version, and it
changes no status. All MCP callers share the reporter label `agent`, so the limit is per server, not per caller. `forge_status` shows
agents only the **count**, because the text was written by another agent. `toolforge problems` also lists repair attempts and the
versions that need you.

## Self-repair

`forge_repair(name, problem, package?)` asks for a new version of an active (or quarantined) tool:

1. With no `package`, the builder model reads the current source, the problem, the failure fingerprints and the stored inputs (all of
   it fenced as untrusted data; the problem is scanned for secrets first) and writes a corrected package. With a `package`, the
   caller's version is the proposal. Either way the loop owns `name`, `parent_version` (the base) and the next free version number.
2. The new version goes through the same gates as any submission, plus the stored inputs in G3.
3. **The old version stays active.** A repaired version waits at `verified_basic` (or `pending_approval`) like any other; you promote it
   with `toolforge promote`. A quarantined base stays quarantined until you do.
4. The attempt is recorded in the ledger as `forge.repaired` (ids, hashes, counts; never the problem text, code or model text).

At most `TOOLFORGE_REPAIR_MAX_PER_HOUR` (default 3) repairs may start on one tool per hour. A base version whose repairs failed three
times in a row is **flagged for you**: further repairs of it are refused and `forge_status` and `toolforge problems` say so. A repair
that could not run because the model or sandbox was unavailable does not count as a failure.

## Rolling back

```
toolforge rollback <name> --to <version> --reason "..."
```

makes a version that was active before active again. The version active now becomes `deprecated`; a quarantined one stays
quarantined. Only a `deprecated` version qualifies (it already passed every gate and approval while it was in service): one that was
never active is refused with the command that does apply (`promote`, `release`, `approvals`), and so is one whose package is gone or
that asks for network or secrets but has no recorded approval for this content. No MCP tool can roll back.
