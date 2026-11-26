# Permissions, approvals, egress and brokered secrets

A tool's manifest can ask for two things that a tool otherwise never has: **network hosts** (`permissions.network`) and **secrets**
(`permissions.secrets`). Neither is ever granted by the tool, by an agent or by an automatic rule. A person grants them, one version at a
time, with `toolforge approvals`. This page says how that works, what the operator reads, and what it does not protect against.

## The flow

1. An agent submits a package whose manifest has non-empty `network` or `secrets`. G1 to G6 run (G5 is skipped when no challenger model is configured).
2. If the package passed, the version does **not** go to `active` and `promote` refuses it. It becomes `pending_approval` (also when it
   is only `verified_basic`; otherwise a network tool could never be used without a model key). An **approval request** is opened.
3. The operator reads the request and answers. Approve: the version becomes `active` (live examples run once first, below). Deny: it is
   `rejected`. A request nobody answers for `TOOLFORGE_APPROVAL_TIMEOUT_S` seconds (default 900) is denied.
4. Whatever happens, the agent sees it through `forge_status` (`pending_approval`, then `active` or `rejected` with what to change). The
   agent cannot approve: the MCP server has no tool for it and an `ast` test (`tests/test_server_tools.py`) keeps it from importing
   one.

A denied, timed-out or failed version is `rejected` for good, like any rejected version: raise `version` and submit again.

## What the operator reads

```text
toolforge approvals list                      # pending requests (--status approved|denied|all)
toolforge approvals show <id>                 # the whole request
toolforge approvals approve <id> --reason "<why>"
toolforge approvals deny <id> --reason "<why>"
```

`show` prints, and the same text is printed again before `approve` and `deny` act:

* a first line **`WEAKER EVIDENCE: G5, G6 did not run`** when gates were skipped (G5 is skipped on a machine with no model configured, or when the model could not be reached),
* the permissions asked for and a **permission diff** against the version that is active now: what is added, kept and removed,
* for each secret, whether a broker service of that name is configured here,
* the tool's description (cleaned and clipped; a warning when it reads like instructions to an AI), one line per gate, and the Blast
  Radius summary,
* for a [browser plan](browser-plans.md), every step of the plan, read from the frozen package, and two extra limits,
* how many `live` examples will run, the limits below, the `code_sha` and the expiry.

The same document is written to `$TOOLFORGE_HOME/approvals/<id>.json` for other tools to read. The registry holds the state; the file
holds what was asked and never changes. Every decision goes to the version's `ledgers/<tool>@<version>/operator.jsonl`
(`forge.approval.granted` with your reason, or `forge.approval.denied`).

Approving refuses, in this order: no reason; a request already decided or timed out; a version that is no longer `pending_approval` or
whose `code_sha` differs from the one in the request; a package that was never built or whose G1 to G4 did not pass; a secret with no broker
service in your environment (nothing is decided, so you can fix it and approve again).

## Live examples

An example in the manifest with `"live": true` is skipped by G3, G4 and G6 (it needs the real service). It runs **once, after you
approve and before the version becomes active**, in a sandbox with the approved hosts and brokered services. Only success is required (its
answer is not compared). If it fails the version is rejected instead of activated. A call made this way is not counted in the version's
statistics. Docker must be available to run `approvals approve` when a version has live examples; otherwise nothing is needed.

## Network: how it is enforced

A tool with no `network` hosts runs with no network at all. A tool with hosts runs behind MiniLocker's egress proxy: the sandbox has no
route out except to that proxy, and the proxy only passes HTTPS `CONNECT` to the allowed hosts, to public addresses only (a name that
resolves to a private or metadata address is refused). Every connection, allowed or blocked, is logged by the proxy and copied into the
call's ledger. At run time the runner also refuses a network tool unless it is `active` **and** the registry holds an approved request
for exactly this `code_sha` and exactly these hosts and secrets.

A package run straight from a folder (`toolforge run --dev-unverified`) never gets a network or a secret; it is refused with a message
saying to submit it.

### What the proxy does not do (read this before approving)

* **One allowlist for every tool.** The proxy cannot tell sandboxes apart. Its allowlist is the union of the hosts of every tool that has
  run since it started, so while tool A runs it can reach hosts approved only for tool B. The Blast Radius report shows the hosts a call
  actually contacted and calls a host the manifest did not declare a **violation**; but the proxy did not stop it.
* **Subdomains pass.** MiniLocker lets through `www.example.org` when `example.org` is allowed.
* **Hosts only, HTTPS on port 443 only.** ToolForge does not inspect what is sent to an allowed host.
* **Two ToolForge processes on one Docker daemon share the proxy's fixed name** and will remove each other's proxy.
* A changed allowlist restarts the proxy, which only happens when no sandbox is using it; otherwise the call fails with "egress is
  busy" and can be retried.

The fix is a per-sandbox allowlist in MiniLocker; see [upstream/minilocker-per-sandbox-allowlist.md](upstream/minilocker-per-sandbox-allowlist.md).

## Brokered secrets

A tool never receives a secret. `permissions.secrets` lists **service names** (for example `github`). The operator configures each
service once, in the environment of the process that runs ToolForge and of the shell that runs `toolforge approvals approve`:

```text
export MINILOCKER_BROKER="github=api.github.com"
export MINILOCKER_BROKER_TOKEN_GITHUB="<a token with the narrowest scope that works>"
```

These are MiniLocker's own variables and are read from the process environment, **not** from `.env` (which holds only `TOOLFORGE_`
settings). A service with no token, or with a malformed name or host, does not exist. Inside the sandbox the tool finds
`MINILOCKER_BROKER_URL` and a placeholder for each service, and calls `$MINILOCKER_BROKER_URL/github/<path>` through the proxy; the
proxy adds the real credential to the request and the token never enters the sandbox.

* A request for a secret with no configured broker service is rejected at once, with the reason; approving it in an environment
  without the service is refused.
* After every call the sandbox's audit is read for **real secrets**. One found inside the sandbox fails the call, discards its output and
  **quarantines** the version. An audit that could not look is recorded as `not_measured`, never as clean.
* Honest limits: the proxy has one broker for all sandboxes on it, so any sandbox on the proxy can ask for any service the proxy was
  started with (only services asked for by tools that have run, not everything you configured). A tool that is allowed to use a
  brokered token can do with it anything the token's own scope allows, and can send what the API returns anywhere its hosts reach. Give
  tokens the narrowest scope.

## What the Blast Radius report adds

For real calls of an approved tool the report compares the connections the proxy logged with the hosts and services the call's own
`forge.call.started` event declared. It shows attempts, allowed, blocked and brokered counts, the hosts contacted, the undeclared hosts
(a violation) and the calls whose proxy log could not be read (a gap, never clean). It also counts the real-secret audit results of the
sampled calls. The report footer repeats the one-allowlist limit.

## Settings

`TOOLFORGE_APPROVAL_TIMEOUT_S` (default 900) is the only setting. Nothing here turns approval off.
