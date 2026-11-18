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
with an `.env.example` line, tests.

## Ranking and confidence

Score = bm25 over name/description/when_to_use/examples (P03) combined with: token overlap between the query and example
inputs, a smoothed success rate (Beta prior, so one lucky call does not dominate), a mild recency term, and a tie-break
for narrower permissions. `forge_find` returns `confidence` (`high`, `medium`, `low`) from calibrated thresholds stored in
one constant block and covered by tests, plus a one-line `why`. Low confidence tells the caller to build or submit.

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

## Acceptance

`pytest -q`.

## Pitfalls

An agent must not be able to reinstate an old vulnerable version or pull a good tool: rollback and release stay operator-only, and reports never act alone. Keep repair
rate-limited so a confused agent cannot spin the registry.
