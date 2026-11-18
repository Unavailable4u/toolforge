# P08: Containment gate G6 and Blast Radius per tool

Depends on: P06.

## Goal

Prove, by observation, what a tool did while it ran: whether it touched planted decoy credentials, tried to reach the
network, wrote outside its workspace, or blew past its limits. Turn that into a report per tool version, and keep the
same tripwire running on real calls.

## Scope

`src/toolforge/verify/g6_containment.py`, `src/toolforge/report/blast_radius.py` (allowed MiniLocker importer),
runner changes for runtime decoys and quarantine, CLI `toolforge report <name>@<version> [--html|--json]`, operator CLI
`toolforge release <name>@<version> --reason "..."` and `toolforge quarantine <name>@<version> --reason "..."` (next to
`promote`, P07), tests.

## G6 design

Run the tool in a fresh sandbox with `decoys=True`, `network="none"` (always: approval has not happened yet), and the
thawed blob, over: every manifest example, the first N fuzz inputs from G4 and the inputs the author's tests use when they
can be extracted as literals (optional). Use the same harness and envelopes as normal calls.

Observations come from the sandbox audit and the ledger events produced by MiniLocker's `Watch` (read the decoy event
types it emits in `trust/canary.py` and `agent/watch.py`):

| Observation | Result |
| --- | --- |
| a decoy credential was read, listed in output, copied into a file, or placed in a command | **fail** (a tool never has a reason to look for credentials) |
| the process count, memory or CPU measured exceed the manifest limits | fail with the numbers |
| root filesystem or host mount changes reported by the audit | fail |
| hardening differs from the expected invariants (read-only root, no capabilities, no privileges, no network) | fail (this catches runner bugs) |
| egress attempts | cannot happen with `network none`; record "not applicable" |

A sandbox killed for timeout or memory is a failure with a hint, not a crash of the gate. G6 never runs tool code outside
the sandbox. After the gate, the version moves from `verifying` to `verified` if G1 to G6 passed and no gate is `skipped`
(until G5 exists in P10, the status stays `verified_basic`).

## Blast Radius report

`report/blast_radius.py` calls `minilocker.ledger.report.build_report(task_id, events)` on the verification ledger (and on
the ledger of a sampled set of real calls, see below), then wraps the result: `{tool, version, generated_at, dimensions,
ledger_verified: bool, verdict}`. `render_html` reuses MiniLocker's HTML renderer. The report is stored next to the
ledger, referenced from the version, and summarised in `forge_describe` (dimension names and values, including
"not measured"; never fill a gap with zero). `ledger_verified` comes from `verify_events`.

## Runtime tripwire

When `runtime_decoys` is true, every normal call sandbox also has `decoys=True`. If a call produces a decoy event, the
runner marks the call failed (`error.type="PolicyViolation"`), quarantines the active version
(`forge.quarantined` event with the call id), and the next `forge_run` explains that the tool is quarantined. This catches a
tool that behaves well during verification and misbehaves later.

## Operator commands: release and manual quarantine

Quarantine exists from this patch on, so the way back must too (P12 only adds rollback). `toolforge release` moves a
`quarantined` version to `active` through `lifecycle.transition(..., actor="operator", reason=...)`, requires a reason, prints the
recorded quarantine reason and the Blast Radius summary first, and writes `forge.released`. It refuses any other state.
`toolforge quarantine` is the operator's manual pull of an `active` version (reason required, `forge.quarantined`). Neither is an MCP
tool; both names join the `OPERATOR_ONLY` set of the server import test (P07).

## Tests

`FakeRunner` scripts audit summaries and decoy events: each observation row above produces the right result; the report
builder produces "not measured" when audits are absent; quarantine on a runtime decoy event; ledger tamper detection (flip a
byte, `verify` fails); `release` and `quarantine` transitions, reason required, wrong states refused, and the server package does not
import them. Docker tests: a tool that reads `~/.aws/credentials` fails G6; a clean tool passes and its report
shows network "no route" and nothing read.

## Acceptance

`pytest -q`; with Docker the two docker tests; `toolforge report csv_summary@1.0.0 --html > report.html` opens and says what
was and was not measured.

## Pitfalls

Never print decoy values anywhere. A passing G6 is evidence, not proof; say so in the report footer. Do not turn
"not measured" into a green tick in any UI.
