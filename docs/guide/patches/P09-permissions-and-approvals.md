# P09: Permissions, approvals, egress and brokered secrets

Depends on: P07, P08.

## Goal

Make the manifest's permissions real: tools reach only the hosts they declared and a human approved, secrets never enter
a sandbox, and approval is a separate, human-only channel.

## Scope

`src/toolforge/core/approvals.py`, `src/toolforge/sandbox/egress_pool.py` (inside the runner module if simpler),
`g7_approval.py`, runner changes (network from manifest, broker placeholders), CLI `toolforge approvals list|show|approve|deny`,
`docs/upstream/minilocker-per-sandbox-allowlist.md`, tests.

## Approval flow (G7)

A version that passed every gate that ran (G1 to G4 and G6, plus G5 when a challenger is configured) and whose manifest has
non-empty `network` or `secrets` becomes `pending_approval`. This includes a version in `verified_basic` (G5 `skipped` because
no model is configured): without that rule a network tool could never become active on the no-key path, since `promote` refuses
it. The request names every skipped gate in its first lines so the operator sees the weaker evidence. An approval request file
is written under `approvals/` with: the full requested permissions, a **permission diff** against the
currently active version of the same tool (added hosts, added secrets, removed ones), the tool description, gate
summaries, and the Blast Radius summary. The operator decides through the CLI (the dashboard in P17 calls the same
functions). Deny, timeout (`approval_timeout_s`) and missing broker configuration all end in `rejected` with a reason.
Versions with no sensitive permission skip G7 and follow `auto_promote` (a `verified_basic` one waits for `toolforge promote`). `toolforge promote` (P07) refuses any version with
network or secrets, so approval is the only way such a version becomes active.

**No MCP tool can approve or promote.** The server process must not import `approvals.decide` or the promote function (a test
enforces it with `ast`). An agent can read approval state through `forge_status` only.

## Runtime enforcement

`ToolRunner` builds the spec from the manifest: `network` is `"none"` when the list is empty, otherwise the tuple of
declared hosts, acquired through the egress pool (see 03). A secret named `github` is valid only if the operator configured
a broker service of that name (`MINILOCKER_BROKER`, `MINILOCKER_BROKER_TOKEN_GITHUB`); the sandbox receives MiniLocker's
placeholder variable and proxy URL (read `broker_note` and how `agent/loop.py` exposes them) and never the token. Add a
real-secret audit: `AuditSummary` must show no real secret was present in the sandbox; if it does, quarantine.

## Live smoke examples

An example with `"live": true` is skipped by G3/G4/G6 (no network at that point) and executed once after approval, in a
sandbox with the approved network, before the version becomes `active`. If it fails, the version is rejected. Tests for
network tools must therefore mock their HTTP layer (the recipe in P14 shows how) and rely on the live smoke for reality.

## Egress pool limits (document them)

One proxy, one global allowlist: the union of build hosts and the hosts of all active approved tools. Consequence: a tool
can reach any host approved for any tool, because the proxy cannot tell tools apart. State this in `docs` and in the
report footer. The upstream design note describes the fix (a per-source-IP allowlist in MiniLocker's proxy, since it already
logs the source of each request) as a task for the MiniLocker repository, not this one.

## Tests

Approval state machine (including `verified_basic` with network going to `pending_approval` with G5 listed as skipped, and `promote` still refusing it); permission diff; timeout denial (inject a clock); `FakeRunner` shows the spec requested for network
tools and `none` for others; the server never imports the decide function; secret requested without broker config is
rejected with an actionable message. Docker: a tool with `network: ["pypi.org"]` can fetch from it and cannot reach an
unlisted host (use a stable local test target if possible, otherwise assert on the proxy log).

## Acceptance

`pytest -q`; with Docker the egress test; `toolforge approvals list` shows a pending request created by a test fixture.

## Pitfalls

Never put token values in manifests, logs, ledgers, approval files or error messages. Approvals are bound to a `code_sha`:
a changed package needs a new approval. Do not let `auto_promote` apply to anything with network or secrets.
