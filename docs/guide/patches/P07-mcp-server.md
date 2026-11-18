# P07: MCP server v1

Depends on: P03, P05, P06.

## Goal

Expose ToolForge to any MCP client. v1 has a small, well-described set of meta-tools; later patches add more. Also give the
operator the one command that makes a verified tool usable before approvals and the challenger exist (`toolforge promote`).

## Scope

`src/toolforge/server/{app.py,tools.py,fences.py}`, `src/toolforge/netguard.py`, CLI `toolforge serve [--transport stdio|http] [--host --port]`,
CLI `toolforge promote`, a minimal `forge_recipe` and `forge_runtime_info`, `docs/clients.md` (config snippets), tests.

## Use the official SDK

Add the official `mcp` Python SDK (pin it). Before writing code, read the installed package: how `FastMCP` declares tools,
how structured results and progress reports work (`Context.report_progress`), how to run stdio and streamable HTTP, and what
the in-memory test client helpers are. If anything differs from this guide, follow the SDK and record it in
`DEVIATIONS.md`.

## Meta-tools (v1)

| Tool | Input | Returns |
| --- | --- | --- |
| `forge_find` | `query: str`, `limit: int = 5` | ranked active tools: name, version, short description, `confidence` (simple thresholds now, refined in P12), `uses`, `success_rate` (or null), permission summary |
| `forge_describe` | `name: str`, `version: str \| null` | manifest (sanitised), examples, status, lineage, gate summaries |
| `forge_run` | `name: str`, `args: object`, `files: object \| null` | the call result (see fencing) |
| `forge_submit` | `package: {manifest, files}` | `VerificationReport`: status, gates with summaries and hints, `next_action` |
| `forge_status` | `name: str`, `version: str \| null` | status and gate summaries, including approval state |
| `forge_list` | `status: str \| null` | tools and versions, most recent first, capped |
| `forge_recipe` | `kind: str` | operator-authored documentation for authors. v1 ships one kind, `package-format` (P14 adds the rest). Size capped and labelled as documentation. |
| `forge_runtime_info` | none | Python version, runtime image, the sandbox limits and the libraries preinstalled in the image. The library list is read from the image when Docker is available and is `null` ("not measured") otherwise. P14 extends it. |

Every description is written for a model: when to call, what you get, what to do on failure. `forge_submit` reports
progress per gate through the SDK's progress notifications.

**`META_TOOLS`** (in `server/tools.py`) is a tuple of `MetaTool(name, summary, inputs, returns, since)`. The server registers its
tools from it, and the README, `docs/clients.md` and the website (P19) are generated from or checked against it. It is the only
list of tools in the repository. Later patches append to it.

`package-format` is written in this patch at `src/toolforge/adapters/recipes/package-format.md`, seeded from
`docs/guide/package-format.md` (P02): the package layout, the harness constraints, the mini test runner (no fixtures, plain
`assert`), `ToolError`, limits, and one complete minimal example. P11 (builder) and every agent author read it.

## Operator command: `toolforge promote`

`toolforge promote <name>@<version> --reason "..."` moves a version in `verified_basic` or `verified` to `active` through
`lifecycle.transition(..., actor="operator", reason=...)` and writes `forge.promoted` with the reason to the ledger. This is the
normal path when no LLM is configured (G5 is `skipped`, so the version stops at `verified_basic`).

* It is **not an MCP tool** and is unreachable from the server process.
* It refuses a version whose manifest has non-empty `network` or `secrets`. Until P09 builds approvals the message says so
  ("tools that ask for network or secrets need approval, which arrives in P09"). P09 later routes those through G7 (straight from
  `verified_basic` as well, so the no-key path works for network tools); promote never bypasses approval.
* It refuses `rejected`, `quarantined` and already `active` versions with a message naming the correct command or state.
* P17 adds the same action to the dashboard by calling the same function.

## Fencing and sanitising (security-critical)

`server/fences.py`: every string that originates from a tool author or a tool run and is returned to the calling model passes
through `textsafety.sanitize`, a length cap (`max_text_chars` 8000 for results, 600 for descriptions), and, for tool
outputs, `fence` (random nonce per response). The response includes one fixed sentence that the fenced block is data
returned by a tool and not instructions. `textsafety.scan` hits add `flags: ["possible_injection"]` to the result without
changing the content. Structured fields (numbers, booleans, enum strings) pass through untouched.

## HTTP transport security

stdio exposes nothing to the network. Put the checks below in `src/toolforge/netguard.py` (loopback test, constant-time bearer check,
`Origin` and `Host` allow-list) so the dashboard (P17) reuses exactly the same code. For `--transport http`:

* Bind `server_host` (default `127.0.0.1`) and `server_port` (default `8765`).
* If the host is not loopback and `server_token` is unset, **refuse to start** with a message that says to set
  `TOOLFORGE_SERVER_TOKEN`.
* When a token is set, require `Authorization: Bearer <token>` on every request and compare in constant time. Answer 401 with no
  detail.
* Validate the `Origin` header against an allow-list (loopback origins plus any the operator configures) and the `Host`
  header against the bound host, to resist DNS rebinding from a web page. Answer 403 otherwise. The MCP transport guidance asks
  servers to do this; confirm what the pinned SDK already does and add only what is missing, recording the difference in `DEVIATIONS.md`.
* The token never appears in logs, errors, the ledger, `toolforge config` output or the process title.

## Behavior rules

* `forge_run` runs only `active` versions. Anything else returns an error that names the status and the next step
  ("version 1.2.0 is pending approval; ask the operator to approve it, then retry").
* `forge_submit` is bounded by `max_concurrent_verifications` (semaphore; excess calls wait up to 30 s then return a clear
  busy error). Resubmitting identical content is idempotent; a different package under an existing `name@version` is an
  error telling the caller to bump the version. A new version of an existing active name must declare
  `parent_version` equal to the active version; otherwise it is rejected as a possible name collision (P12 finishes this
  rule; implement the check now).
* **There is no MCP tool that approves, promotes, releases, rolls back or deletes anything.** Those are operator actions (CLI and
  dashboard). A test asserts the exact tool list so nothing is added by accident. The session tools added in P13
  (`forge_session_close`, `forge_session_rewind`) act only on the caller's own sessions and change no registry state, so they are
  not exceptions to this rule.
* Errors are returned as MCP tool errors with an agent-readable message and a correlation id, never a stack trace.

## Optional direct exposure

When `expose_direct` is on, each active tool is also registered as an MCP tool named `tf_<name>` with its `input_schema`
and sanitised description, and the server sends the tools-list-changed notification when a tool is promoted, deprecated or
quarantined. Write a small spike test first to confirm the SDK supports adding and removing tools at runtime and emitting
the notification. If it does not, leave this off, say so in `DEVIATIONS.md`, and keep only the dispatcher tools.

## Tests

In-memory client and server: list tools (exact set equals `META_TOOLS`), `forge_submit` with scripted `FakeRunner` results moving
a package to `verified_basic`, `toolforge promote` then `forge_find` and `forge_run`, `forge_run` on a non-active version refused,
fencing wraps hostile output ("ignore previous instructions" in a tool result is fenced and flagged), oversized outputs clipped,
idempotent resubmit, version immutability, concurrency semaphore. `forge_recipe` returns `package-format` and rejects unknown
kinds with the list of valid ones; `forge_runtime_info` reports `null` libraries when Docker is absent (never an empty list).
Promote: refuses network or secrets, refuses wrong states, requires a reason, writes the ledger event, and the server package does
not import it. That `ast` test keeps one constant, `OPERATOR_ONLY`, naming the functions the server package may never import
(`promote` now; P08 adds `release` and `quarantine`, P09 `decide`, P12 `rollback`; each patch extends the constant and its test). HTTP security: refuses a non-loopback host without a token; wrong or
missing token gives 401; bad `Origin` gives 403; the token is absent from every log line captured in the test. A drift test
compares the tool table in `docs/clients.md` with `META_TOOLS`.

## Acceptance

`pytest -q`; `toolforge serve` over stdio answers a `tools/list` request (a test or a documented manual command); with Docker,
`toolforge submit examples/toolpacks/csv_summary`, then `toolforge promote csv_summary@1.0.0 --reason "local dev"`, then a client
can `forge_find` and `forge_run` it.

## Pitfalls

Do not print anything to stdout in stdio mode except protocol messages (log to stderr or a file). Do not let the SDK's
default error formatting leak paths. Do not return raw gate `detail` blobs; return summaries and hints. Do not let `promote`
become a way around approval.
