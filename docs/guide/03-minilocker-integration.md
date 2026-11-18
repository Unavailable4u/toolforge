# Using MiniLocker wisely

MiniLocker (https://github.com/shaikh-dotcom/MiniLocker) is a sandbox for AI agents: hardened Docker containers with no
route out, an allowlisting egress proxy, a credential broker, decoy credentials, snapshots, a hash-chained ledger and a
"Blast Radius" report. ToolForge reuses that machinery for the one thing it is best at: **running untrusted code and
proving what it did**. ToolForge does not reuse MiniLocker's agent loop, its HTTP control plane, its policy engine for
shell commands, its scenarios, its artifact store or its UI.

Reviewed commit: `0e76295a986292cdb07795d6efc1570f9d0c1acb` (2026-11-07). This document describes that commit. The
real source at the pinned commit always wins over this text; if they differ, follow the source and write it down in
`docs/guide/DEVIATIONS.md`.

Checked on 2026-11-19 from the public repository page: it exists, is MIT licensed, and its `backend/minilocker/` layout has the
`agent/`, `policy/`, `sandbox/`, `egress/`, `ledger/` and `api/` areas this guide relies on. **Not checked:** that the pinned commit
below is the one this text was written against, and every function signature in the table below. P04 reads the source at the pin
before writing the adapter. MiniLocker also ships an "Attack Lab" of its own (scripted attacks against its agent sandbox);
ToolForge's Attack Lab (P15) is a separate suite aimed at hostile tool authors and the verification pipeline.

## How it is consumed

* A git submodule at `third_party/minilocker`, pinned to one commit (the commit above unless the guide says otherwise).
  MiniLocker has no `pyproject.toml`, so ToolForge puts `third_party/minilocker/backend` on `sys.path` in exactly one
  place: `src/toolforge/sandbox/_ml_path.py`. `TOOLFORGE_MINILOCKER_PATH` overrides the location.
* Its Python requirements that matter here are `docker` and, for the broker/egress path, nothing beyond the standard
  library. Do not install MiniLocker's FastAPI, boto3 or moto requirements for ToolForge.
* **Small seam.** Only the modules in `ALLOWED_IMPORTERS` may `import minilocker`: `sandbox/_ml_path.py`,
  `sandbox/minilocker_runner.py`, `ledger.py` and `textsafety.py` (P04), plus `report/blast_radius.py` (P08) and
  `sandbox/browser_adapter.py` (P14) when those patches add them. A test (`tests/test_import_boundary.py`, P04) fails if
  any other module imports it. Everything else talks to the
  `SandboxRunner` protocol, which also has a pure-Python `FakeRunner` for unit tests.
* Nothing from MiniLocker is copied into this repository. If MiniLocker needs a change, change it in its own
  repository, then move the submodule pin and note it in `PROGRESS.md`.

## What we use, mapped to what we need

| Need | MiniLocker piece (module) | Facts at the reviewed commit |
| --- | --- | --- |
| A hardened place to run tool code | `Sandbox` (`minilocker.sandbox.docker_provider`) | `Sandbox(image="python:3.12-slim", workspace_mb=64, egress=None, *, role="code", command="sleep infinity", mem_limit="512m", pids_limit=128, tmp_mb=None, shm_size=None, extra_env=None)`. Runs as uid 65534, read-only root filesystem, all capabilities dropped, `no-new-privileges`, one CPU, memory without swap, `network_mode="none"` unless an egress manager is passed. Writable only through tmpfs: `/workspace` (exec allowed, size-capped) and `/tmp` (no exec). |
| Run a command with a hard deadline | `Sandbox.exec(cmd, timeout_s)` | Runs `sh -c cmd`; on timeout the **whole container is killed** and `dead` is set. Returns `ExecResult(exit_code, stdout, stderr, timed_out)`. |
| Run argv, binary-safe, output capped | `Sandbox.exec_capped(argv, timeout_s, max_bytes)` | Returns `RawResult(exit_code, stdout, stderr, timed_out, truncated)`. Use this for anything ToolForge parses. |
| Feed bytes to a process | `Sandbox.exec_stdin(argv, data, timeout_s, max_out)` | No shell. Use it to place input and to restore blobs. |
| Freeze and restore a workspace | `minilocker.sandbox.snapshot.capture(box)` and `restore(box, data)` | Gzipped tar of `/workspace`. This is how a built tool package becomes a blob and is re-materialised in a fresh sandbox. |
| Measure what a sandbox did | `Sandbox.measure()`, `Sandbox.hardening()` | Highest memory, process count and CPU; the hardening settings as Docker reports them. |
| Controlled outbound traffic | `EgressManager` (`minilocker.egress.manager`) | `EgressManager(allowed_hosts, image=..., packaged=False, broker=None).start()`. Provides `internal_network` and `proxy_env()`; every attempt is logged per container. **One proxy at a time**: network and container names are fixed constants. The allowlist is set when the proxy starts. |
| Secrets without exposure | credential broker (`broker_from_env`, `MINILOCKER_BROKER`, `MINILOCKER_BROKER_TOKEN_<n>`) | The sandbox holds a placeholder; only the proxy holds the token and makes the HTTPS call. HTTP APIs only. |
| Catch credential theft | `Watch` (`minilocker.agent.watch`) | `Watch(strict=False)`; `supported(box)`, `attach(box, log)` plants decoy SSH/AWS/GitHub/.env credentials, `after_tool(box, tool, result, log)` notices reads and copies, `on_egress(ev, log)`, `audit(box, kind, log, workspace=True)` records filesystem, secrets and compute at destroy time. `log` is `log(actor, type, payload)`. `tripped` becomes true when a decoy is used in strict mode. |
| Tamper-evident history | `Ledger`, `verify_events`, `read_events` (`minilocker.ledger.chain`) | `Ledger(task_id, path).append(actor, type, payload)` hash-chains events; `verify_events(events)` returns `(ok, message)`. |
| The report | `build_report(task_id, events, read_error=None)` and `render_html(report)` (`minilocker.ledger.report`) | Nine dimensions measured from ledger events. A dimension with nothing behind it says "not measured". |
| Untrusted text handling | `sanitize`, `scan`, `fence` (`minilocker.policy.injection`) | `sanitize` strips invisible Unicode, `scan` scores instruction-like text, `fence` wraps text in a random-nonce marker. Early warning, not a security boundary. |
| Browser for tools that need a web page | `BrowserSandbox` (`minilocker.sandbox.browser`) | `BrowserSandbox(egress=None, image=None, ...)`, `.call(action, params, timeout_s)`, `.destroy()`, `BrowserUnavailable`, `image_present()`. Read the actions in `browser_worker.py` before use. |

Before writing the adapter, open and read these files at the pinned commit. Do not rely on this table alone:
`sandbox/docker_provider.py`, `sandbox/snapshot.py`, `sandbox/measure.py`, `egress/manager.py`, `agent/watch.py`,
`ledger/chain.py`, `ledger/report.py`, `policy/injection.py`, and the functions `flush_egress` and `apply_rewind` in
`agent/loop.py` (they show how egress events are forwarded to `Watch` and how a sandbox is rebuilt from a snapshot).

## Seam interface (defined in P04)

```python
class SandboxRunner(Protocol):
    def open(self, spec: SandboxSpec) -> SandboxHandle: ...
class SandboxHandle(Protocol):
    def put(self, path: str, data: bytes) -> None: ...            # exec_stdin underneath
    def run(self, argv: list[str], timeout_s: int, max_out: int) -> RunOutput: ...
    def freeze(self) -> bytes: ...                                 # snapshot.capture
    def thaw(self, blob: bytes) -> None: ...                       # snapshot.restore
    def events(self) -> list[dict]: ...                            # ledger events so far (incl. egress, audits)
    def close(self) -> AuditSummary: ...                           # audit + destroy, returns what was measured
```

`SandboxSpec` carries image, network rule (`none` or a list of hosts), workspace and memory limits, environment, the
`decoys` flag and a ledger path. All MiniLocker-specific behavior (Watch, egress forwarding, hardening capture, audits)
lives inside the runner implementation.

## Things MiniLocker does not do that ToolForge must handle

1. **Per-sandbox egress allowlists.** The proxy has one global allowlist. v1 keeps one `EgressManager` whose allowlist is
   the union of hosts approved for active tools plus the build allowlist, and documents the consequence: a tool can
   reach any host approved for any tool. The proper fix is an upstream MiniLocker change (the proxy already knows the
   source IP of each request, so a per-source allowlist is natural). Described as an optional upstream task in P09.
2. **Non-HTTP protocols.** The proxy accepts HTTPS `CONNECT` only. Tools that need a database server over its wire
   protocol are out of scope; data-file tools (SQLite, DuckDB, CSV) are in scope.
3. **Persistent state.** A sandbox's workspace dies with it. Stateful tools use sessions (P13) built on `freeze`/`thaw`.
4. **Package installs.** The sandbox is read-only apart from tmpfs. Dependencies are either baked into the runtime image
   (P14) or installed during the build gate into `/workspace` through the egress proxy (PyPI only), then frozen.
5. **Vision.** MiniLocker sends screenshots to its UI, not to a model. Do not design features that need an image
   understanding step in v1.
6. **Shell policy.** The regex policy engine scores shell commands from a live agent. ToolForge does not run an agent's
   shell, so it does not use that engine; its static gate (G1) is separate and equally advisory.

## Rules of use

* Every tool call and every verification run uses a **fresh sandbox**, except sessions (P13).
* Never mount host paths. Pass data in with `put` and out through the result envelope.
* Always close handles in a `finally` block; a leaked container is a bug. `toolforge doctor` (P04) lists and removes
  leftover `minilocker-*` containers owned by this process.
* Real secrets never enter a sandbox. Brokered services are named in the manifest; the sandbox gets placeholders only.
* If Docker is unavailable, runner construction raises `SandboxUnavailable`; callers turn it into a `skipped` gate or a
  clear MCP error. Never fall back to running tool code on the host.
