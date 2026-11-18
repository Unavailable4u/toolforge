# P05: Execution contract and `run` path

Depends on: P02, P04. (Uses the registry from P03 only for stats; `ToolRunner` takes a registry-like object so it can be tested alone.)

## Goal

Define precisely how one tool invocation happens inside a sandbox and what comes back, then implement the host side that
drives it.

## Scope

`src/toolforge/sandbox/harness.py` (the in-sandbox script, kept as a string constant plus a copy under
`src/toolforge/sandbox/assets/` that tests compare for equality), `src/toolforge/sdk.py` (the tiny module tools may import),
`src/toolforge/core/runner.py`, `src/toolforge/models/results.py` additions (`CallResult`), CLI `toolforge run`, tests.

## What a tool looks like to its author

```python
# tool.py
from toolforge_sdk import ToolError

def run(args: dict) -> object:
    """Return any JSON-serialisable value. Raise ToolError(message) for expected failures."""
    ...
```

`toolforge_sdk` is injected into the sandbox next to the package; it contains `ToolError`, `input_path(name)` (resolves a
caller-provided file under `/workspace/inputs/`), and `output_path(name)` (under `/workspace/outputs/`, reserved for P14).
Document these in the package-format guide.

## Sandbox layout and protocol

```
/workspace/
  pkg/            the thawed package (tool.py, tests/, .deps/ if any)
  inputs/         caller files for this call (only if permissions.inputs)
  .tf/harness.py  the harness, written by the host
  .tf/in.json     {"args": {...}, "entrypoint": "tool.py:run", "limits": {...}}
  .tf/toolforge_sdk.py
```

The host runs `python /workspace/.tf/harness.py <nonce>`. The harness:

1. Adds `/workspace/pkg` and `/workspace/pkg/.deps` and `/workspace/.tf` to `sys.path`; sets `PYTHONHASHSEED=0`,
   `PYTHONDONTWRITEBYTECODE=1`; applies `resource` limits (CPU seconds, file size) where they cannot conflict with Docker's
   own memory limit.
2. Redirects `sys.stdout` and `sys.stderr` to bounded buffers while the tool runs.
3. Imports the entrypoint module and calls the function with `args`.
4. Writes one envelope to the real stdout as `<<TF:{nonce}>>{json}<<TF-END:{nonce}>>`:

```json
{"ok": true, "result": <json>, "error": null, "stdout_tail": "...", "stderr_tail": "...", "duration_ms": 12, "truncated": false}
{"ok": false, "result": null, "error": {"type": "ToolError|Crash|OutputTooLarge|NotJson", "message": "...", "trace_tail": "..."}, ...}
```

`ToolError` is a handled failure (`type: "ToolError"`); any other exception is `Crash`; a result that is not JSON or
exceeds `output_kb` is `NotJson` or `OutputTooLarge`. Tails are clipped to 2000 characters each.

A tool can print the delimiter or write to file descriptor 1 directly and so forge a result envelope. That only lets a tool
say a different result for itself, which it could return legitimately, so it is not an escalation. The host still treats
the envelope as untrusted data: schema-check it, cap it, never `eval` it.

## Host side: `ToolRunner`

```python
class ToolRunner:
    def __init__(self, runner: SandboxRunner, registry, settings, ledger_factory): ...
    def run(self, name_or_version, args: dict, *, files: dict[str, bytes] | None = None,
            session_id: str | None = None, allow_status=("active",)) -> CallResult
```

Steps: resolve version and check status; validate `args` against the manifest `input_schema` (fail fast, no sandbox);
validate and size-check `files` (at most 20 files, 1 MB total, safe relative paths, only when `permissions.inputs`);
build the `SandboxSpec` from the manifest (network from permissions in P09; for now `none`); open the sandbox, `thaw` the blob,
`put` harness, sdk, input and files; `run` with the manifest timeout plus a small grace; parse the envelope between the nonce
markers (last occurrence wins, anything else in stdout is discarded); `close`; record the call; return `CallResult`:

```python
CallResult(ok, result, error, stdout_tail, stderr_tail, duration_ms, version, call_id, truncated, sandbox_timed_out)
```

Failure mapping: container died or timed out gives `error.type="Timeout"` or `"SandboxCrashed"`; no envelope gives `"NoResult"`
with a sanitised stderr tail; harness import errors give `"Crash"`. Never leak host paths or Docker details to the caller.

## CLI

`toolforge run <name> --args '{...}' [--file path ...]` and `toolforge run --package <dir> --args '{...}' --dev-unverified`
(builds a blob on the fly, prints a warning that nothing was verified, still runs only in the sandbox).

## Tests

* Harness logic without Docker: a test helper may execute `harness.py` through `subprocess` **only** for fixtures that are
  checked into `tests/fixtures/trusted_tools/` (our own code), never for generated packages. Cases: success, ToolError,
  crash, non-JSON result, oversized result, tool that prints the delimiter, tool that prints a lot, unicode.
* `ToolRunner` with `FakeRunner`: schema rejection never opens a sandbox; file path traversal rejected; envelope parsing
  (forged marker with the wrong nonce ignored); timeout mapping; stats recorded; sandbox closed on every path.
* Docker (`docker` marker): the reference packs run end to end; a tool that reads another call's data finds nothing.

## Acceptance

`pytest -q`, then with Docker `toolforge run --package examples/toolpacks/word_count --args '{"text": "a b c"}' --dev-unverified`.

## Pitfalls

Do not import tool code in the host process under any circumstances. Do not trust `duration_ms` from the envelope for
stats; measure on the host. Keep the harness dependency-free (standard library only).
