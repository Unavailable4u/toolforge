# P04: Sandbox seam and MiniLocker runner

Depends on: P01.

## Goal

Everything that needs a sandbox talks to one small interface. One implementation uses MiniLocker; one is a scripted fake
for unit tests. MiniLocker is added as a pinned submodule and is imported in a handful of named modules only.

## Scope

* `third_party/minilocker` (git submodule) and `.gitmodules`.
* `src/toolforge/sandbox/{__init__.py,base.py,fake.py,minilocker_runner.py,_ml_path.py}`, `src/toolforge/ledger.py`,
  `src/toolforge/textsafety.py`.
* CLI: `toolforge doctor` becomes real.
* `tests/test_import_boundary.py`, `tests/test_fake_runner.py`, `tests/docker/test_minilocker_runner.py` (marker `docker`).

## Adding the submodule

```
git submodule add https://github.com/shaikh-dotcom/MiniLocker.git third_party/minilocker
git -C third_party/minilocker checkout 0e76295a986292cdb07795d6efc1570f9d0c1acb
git add .gitmodules third_party/minilocker
```

If your environment cannot reach GitHub, do not fake it: commit everything else, state clearly in your reply that the
person must run the three commands above, and make `_ml_path.py` raise a `SandboxUnavailable` with that exact advice
when the directory is empty. Never copy MiniLocker files into the repo.

## Interface (`base.py`)

```python
@dataclass(frozen=True)
class SandboxSpec:
    image: str
    network: Literal["none"] | tuple[str, ...]     # "none" or the hostnames reachable through the egress proxy
    workspace_mb: int = 64
    memory_mb: int = 256
    pids: int = 128
    env: Mapping[str, str] = field(default_factory=dict)
    decoys: bool = False                           # plant MiniLocker decoy credentials (Watch)
    ledger_path: str | None = None
    label: str = ""                                # free text for ledger events: "verify:csv_summary@1.0.0:G3"

@dataclass
class RunOutput: exit_code: int; stdout: bytes; stderr: bytes; timed_out: bool; truncated: bool
@dataclass
class AuditSummary: hardening: dict; measured: dict; decoy_events: list[dict]; egress_attempts: list[dict]; ledger_path: str | None; destroyed: bool

class SandboxHandle(Protocol): put, run, freeze, thaw, events, close   # see 03-minilocker-integration.md
class SandboxRunner(Protocol): open(spec) -> SandboxHandle
```

`SandboxUnavailable` is raised by `open` when Docker, the image or the MiniLocker submodule is missing. Handles are
context managers; `__exit__` always calls `close`.

## `FakeRunner`

A scripted runner for unit tests. It never executes any code. Tests register expectations: a function
`(argv, put_files) -> RunOutput`, or a list of canned outputs. It records every `open(spec)` so tests can assert the
network rule, limits and decoy flag that production code requested. `freeze` returns a deterministic tar of what was `put`.

## `MiniLockerRunner`

Implements the protocol on `Sandbox`, `EgressManager`, `Watch`, `Ledger`, `snapshot` (read the real signatures first).

* `open`: build `Sandbox(image=..., workspace_mb=..., mem_limit=f"{memory_mb}m", pids_limit=..., extra_env=..., egress=<pool-managed manager or None>)`.
  If `decoys`, create `Watch(strict=False)`, call `attach(box, log)`. `log(actor, type, payload)` appends to a `ToolforgeLedger`.
  Log `sandbox.created` with `hardening()` exactly as MiniLocker's loop does so its report builder understands the file.
* Egress: an `EgressPool` object owns the singleton `EgressManager`. `acquire(hosts)` returns the manager when `hosts` is a
  subset of the current allowlist, restarts it with the union only when no sandbox currently uses it, and raises
  `SandboxUnavailable("egress is busy")` otherwise. `release()` decrements. `network == "none"` never touches the pool.
* Egress events: replicate what `flush_egress` in MiniLocker's `agent/loop.py` does (forward proxy events for this container
  to `Watch.on_egress` and to the ledger) at the end of every `run` and in `close`.
* `run(argv, timeout_s, max_out)`: `exec_capped`. After each run call `watch.after_tool(...)` when decoys are on, with a
  short textual rendering of the result.
* `freeze`/`thaw`: `snapshot.capture` / `snapshot.restore`. `thaw` must also verify extraction succeeded.
* `close`: `watch.audit(box, "code", log)`, then `destroy()`, return `AuditSummary` assembled from the audit events and
  `hardening()`. Always destroys, even when the audit raises.
* Threading: one handle is used by one thread at a time. The runner is safe to use from several threads.

`ledger.py` wraps `minilocker.ledger.chain.Ledger` as `ToolforgeLedger(path, task_id)` with `append(actor, type, payload)`,
`events`, `verify()`; it creates parent directories and fsyncs after each append. `textsafety.py` wraps
`sanitize/scan/fence` and falls back to a small pure-Python implementation (strip control and invisible characters, cap
length, random-nonce fence) when MiniLocker is not importable, so the server and unit tests work without the submodule.

## Import boundary

`tests/test_import_boundary.py` walks `src/` with `ast` and fails if any module outside
`ALLOWED_IMPORTERS = {sandbox/_ml_path.py, sandbox/minilocker_runner.py, ledger.py, textsafety.py}` imports `minilocker`
(later patches may extend the set with `report/blast_radius.py` and `sandbox/browser_adapter.py`, in their own patch and
with a line in `DEVIATIONS.md`-style notes inside the test file). It also asserts that `minilocker` is imported lazily
(inside functions or guarded), so `import toolforge` never requires Docker.

## `toolforge doctor`

Checks and prints: Python version, Docker daemon reachable, runtime image present (and its digest), submodule present and
at the pinned commit, egress proxy image pull ability (skip if offline), leftover `minilocker-*` containers (offer
`--clean` to remove those labelled `minilocker=sandbox` that are older than 10 minutes), data directory writable. Exit
code 1 if any required check fails. Checks are small functions registered in one list so later patches add theirs without
editing a long function: P10 adds the warning "challenger model equals builder model", P13 makes `--clean` also remove session
containers.

## Tests

Unit tests (no Docker): the import boundary; `FakeRunner` records every spec, never runs code and freezes deterministically;
`_ml_path.py` raises `SandboxUnavailable` with the three-command advice when the submodule directory is empty; `doctor` exit
codes and output with a faked Docker client; the `textsafety` fallback strips invisible characters, caps length and fences with a
random nonce.

Docker tests (marker `docker`): open a sandbox; `put` then `run` a script; network is unreachable (a socket connect fails); the root filesystem is
read-only (writing `/etc/x` fails) while `/workspace` is writable; `freeze` then `thaw` into a new sandbox round-trips a
file; a command that sleeps longer than its timeout returns `timed_out=True` and the container is gone; `close` returns an
`AuditSummary` whose `hardening` shows `network_mode == "none"`, `read_only_rootfs` true and all capabilities dropped;
with `decoys=True`, a command that prints the planted AWS key file produces a decoy event.

## Acceptance

`pytest -m "not docker" -q` passes; with Docker: `pytest -m docker tests/docker -q`; `toolforge doctor`.

## Pitfalls

Do not wrap MiniLocker's `run_task` or its API. Do not hold sandboxes open across calls (sessions are P13). Do not log
decoy values or any environment contents. Keep the fake honest: it must not run code.
