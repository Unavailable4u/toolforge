# P12: Reuse ranking, self-repair, rollback

Depends on: P07, P11.

## Goal

Make the registry a toolbox that gets better: find the right tool quickly, notice when a tool fails, repair it through the
same gates, and let the operator roll back.

## Scope

`src/toolforge/registry/search.py` (ranking), `src/toolforge/core/repair.py`, registry migration for `regression_cases` and
`problem_reports`, MCP tools `forge_repair` and `forge_report_problem` (both appended to `META_TOOLS`; the README tool table,
`docs/clients.md` and the exact-tool-list test change in the same patch), CLI `toolforge rollback` (`release` and manual
`quarantine` already exist since P08), new settings `repair_max_per_hour` (default 3) and `store_failed_inputs` (default false), each
with an `.env.example` line, `src/toolforge/server/signature.py` (typed call signatures, below), `src/toolforge/server/budget.py` (response
size budget, below), `tests/test_signature.py`, `tests/test_context_budget.py`, tests.

## Ranking and confidence

Score = bm25 over name/description/when_to_use/examples (P03) combined with: token overlap between the query and example
inputs, a smoothed success rate (Beta prior, so one lucky call does not dominate), a mild recency term, and a tie-break
for narrower permissions. `forge_find` returns `confidence` (`high`, `medium`, `low`) from calibrated thresholds stored in
one constant block and covered by tests, plus a one-line `why`. Low confidence tells the caller to build or submit.

## Typed signatures

`forge_describe` (only it; `forge_find` stays small) gains a `signature` field, so a model that writes code which calls a tool has the
exact call shape without reading a JSON Schema:

```json
"signature": {
  "python": "def csv_summary(path: str, delimiter: str = \",\") -> dict[str, Any]: ...",
  "typescript": "declare function csv_summary(args: { path: string; delimiter?: string }): Promise<{ rows: number }>;",
  "status": "generated", "notes": []}
```

* `server/signature.py` builds it from the manifest's `input_schema` and `output_schema` and nothing else, with a small deterministic
  translator: `string`, `integer`, `number`, `boolean`, `null`, `array` with `items`, `object` with `properties` and `required` (depth at
  most 4), `enum` as a literal union, `default` for Python. Required parameters come first, in schema order, then optional ones in schema order.
* Anything else (`oneOf`, `anyOf` other than "type or null", `$ref`, `patternProperties`, a schema in `additionalProperties`, `format`)
  becomes `Any` / `unknown`, the status is `partial`, and `notes` names the first construct it did not translate. No usable schema gives
  status `not_available` and both stubs `null`. A stub is never guessed.
* **The schema is hostile input from a tool author, so no author text is ever copied into a code position.** A property becomes a named
  parameter only if it matches `^[A-Za-z_][A-Za-z0-9_]{0,63}$` and is not a Python or TypeScript keyword; if any property fails that, the
  stub takes one `args: dict[str, Any]` / `args: Record<string, unknown>` and a note says why. Descriptions, titles and examples never
  appear in a stub (no comments, no docstrings). An enum value is emitted only as a JSON-escaped string literal of at most 40 printable
  characters after `textsafety.sanitize`, at most 12 values; otherwise that field is plain `str` / `string` and the status is `partial`.
  Each stub is at most 2000 characters; a larger one is replaced by the plain `dict` form and the status is `partial`.
* A stub **describes** how to call a tool. It is not an importable module and it runs nothing: tools still run only through `forge_run`
  (or `forge_chain` if S02 is built).
* The response keeps its existing `flags` and `injection_scan`, which come from scanning the description and manifest text as before.

## Response size budget

`forge_find` exists so that a model never has to load every tool. Guard it. `server/budget.py` holds one constant block (`BUDGET`) and
`estimate_tokens(text)`, which is `ceil(len(text) / 4)`. It is an **estimate** (there is no tokenizer in the dependencies) and every
place that shows one says so; P16 can replace it with counts from a real endpoint. The block fixes, in characters:

* the whole `tools/list` payload of the server with `expose_direct` off, which must not change with the number of tools in the registry;
* one `forge_find` response at the default `limit` (5), which must not depend on the registry size either;
* one `forge_describe` response including its `signature`.

`tests/test_context_budget.py` registers 10 and then 200 synthetic active tools (scripted gates, no Docker) and fails when a response
is larger than its budget or when the `tools/list` payload differs between the two registries. A change that needs more room edits the
budget **and** adds a line to `DEVIATIONS.md`, so the cost of a bigger response is a decision and not an accident.

## Failure capture

A call that ends in `Crash`, `Timeout`, `SandboxCrashed`, `NoResult` or `PolicyViolation` is recorded with error class and a **shape fingerprint** of the
input (types and sizes, not values). Raw failing inputs are stored only when `store_failed_inputs` (`TOOLFORGE_STORE_FAILED_INPUTS`) is true and the
input is under 8 KB; they become `regression_cases` used by later versions' gates. State the privacy trade-off in the docs.

## Repair

`forge_repair(name, problem: str, package: {...} | null)`:

* With `package`, treat it as a new version proposed by the caller (`parent_version` = active).
* Without it, call the builder (P11) with the current source, the problem, the failure fingerprints and the regression cases.
* Run the full pipeline plus the regression cases as an extra G3 input. On success the new version follows the usual rules
  (so with no challenger it waits at `verified_basic`, and with new network hosts it waits for approval). The old version stays
  `active` until the new one is promoted; the lifecycle, not repair, then deprecates it. Record `forge.repaired` with parent and child.

Limit repair attempts per tool per hour. A version that fails repeatedly is flagged for the operator instead of retrying.

## Quarantine and problem reports

`forge_report_problem(name, version, description)` stores a report (sanitised, capped, rate-limited per caller) and shows it to the
operator through `forge_status`, the CLI and the dashboard. **A report never quarantines anything by itself**: reports come from an
agent that may be confused or hostile, and letting them pull a tool would let one caller disable it for everyone.

Automatic quarantine uses measured outcomes only: a failure rate above 40 percent over at least 10 of the last 20 calls, or any
`PolicyViolation`. A *failure* here is `Crash`, `Timeout`, `SandboxCrashed` or `NoResult`; a handled `ToolError` and a call rejected
by schema validation are the caller's problem and do not count. Quarantine clears the active pointer, closes the tool's sessions
(once P13 exists) and emits `forge.quarantined`. Only the operator can release (`toolforge release`, reason required, P08).

## Rollback and name safety

`toolforge rollback <name> --to <version> --reason "..."` makes a **previously active** (`deprecated`) version active again; the
current active version, if any, becomes `deprecated` (a quarantined one stays `quarantined`). "Previously active" is the right test
because such a version already passed every gate and any approval it needed (approvals are bound to `code_sha`, so none is repeated),
and it also covers versions the operator promoted by hand from `verified_basic`. A version that was never active is refused with a
message naming `toolforge promote`. The reason is recorded and the ledger gets `forge.rolled_back`. Not available over MCP. Name collision rule: a
submission for an existing active name must be a higher version whose `parent_version` equals the active one, otherwise
it is rejected; a near-duplicate with a different name triggers a `similar_to` hint rather than a block.

## Tests

Ranking order on a fixed corpus; confidence thresholds; Beta smoothing behavior; failure fingerprint contains no values;
regression cases applied; auto quarantine triggers at the threshold and not before, and ignores `ToolError`s; problem reports never quarantine; rollback transitions
and ledger event, and refusal of a never-active version; the exact `META_TOOLS` list includes the two new tools;
collision rule; repair loop with `FakeLLM` where the first repair fails a regression case and the second passes.

Signatures (`tests/test_signature.py`): a golden stub for each example tool pack; one case for every schema construct in the list above
(translated, or `partial` with a note); hostile property names (`a-b`, `class`, `__import__('os')`, `x*/y`, `a b`, a newline, a name of
10,000 characters, non-ASCII) all fall back to `args` and never reach a stub; hostile enum values (quotes, backslash, `*/`, newline,
a very long value) stay escaped or are dropped; the output is deterministic; the Python stub passes `ast.parse` and contains only names
from the schema, `Any`, `dict`, `list`, `str`, `int`, `float`, `bool`, `None`; the TypeScript stub matches a small regular grammar
(no compiler is needed); the 2000 character cap; `forge_describe` over MCP carries the field and `forge_find` does not.
Budget (`tests/test_context_budget.py`): as described above.

## Acceptance

`pytest -q`.

## Pitfalls

A signature must never carry a sentence an author wrote: a model reads it as code, and a comment in it would be an instruction channel with
no fence around it. Do not add a description to a stub "to be helpful". An agent must not be able to reinstate an old vulnerable version or pull a good tool: rollback and release stay operator-only, and reports never act alone. Keep repair
rate-limited so a confused agent cannot spin the registry.
