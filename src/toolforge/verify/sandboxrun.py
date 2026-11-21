"""What gates G2, G3 and G4 share: opening a sandbox for a package, calling the harness in one of its modes, reading what comes back.

Everything the sandbox prints is hostile input, because it was produced by code the author wrote. The envelope is parsed strictly
(`services.runner.parse_envelope`, the same code the call path uses), and the report inside it is read field by field with every
number clamped and every text clipped and cleaned before a gate may put it in a hint. A forged or malformed report becomes
"no usable report", which fails the gate; it can never make one pass (`tests/test_sandboxrun.py`).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from toolforge.errors import SandboxUnavailable
from toolforge.sandbox import harness
from toolforge.sandbox.base import SandboxHandle, SandboxSpec
from toolforge.services.runner import (
    ENVELOPE_OVERHEAD,
    WORKSPACE_MB,
    parse_envelope,
    repack_blob,
    sdk_source,
)
from toolforge.verify.pipeline import GateContext, clip

MAX_OUT_BYTES = 8 * 1024 * 1024
GRACE_S = (
    5  # the host's wall clock allows this much beyond what the harness was told, for start-up and reporting
)
_ID_CHARS = 160
_ERROR_CHARS = 300
MAX_LISTED = 20  # failures and errors a test report may list (the harness lists at most this many)


def limits_of(ctx: GateContext) -> dict[str, int]:
    """The package's own limits with the settings' defaults filled in, as a call would use them."""
    declared = ctx.package.manifest.limits
    return {
        "timeout_s": declared.timeout_s or ctx.settings.default_timeout_s,
        "memory_mb": declared.memory_mb or ctx.settings.default_memory_mb,
        "output_kb": declared.output_kb or harness.DEFAULT_OUTPUT_KB,
    }


def open_sandbox(
    ctx: GateContext, gate: str, *, network: Any = "none", memory_mb: int | None = None
) -> SandboxHandle:
    """A new sandbox for one gate. Raises `SandboxUnavailable` when there is none (the operator's problem, not the package's)."""
    if ctx.runner is None:
        raise SandboxUnavailable(
            "verification needs a sandbox and none is configured",
            code="no_sandbox",
            hint="run `toolforge doctor`; gates G2 to G4 cannot run without Docker",
        )
    spec = SandboxSpec(
        image=ctx.settings.runtime_image,
        network=network,
        workspace_mb=WORKSPACE_MB,
        memory_mb=memory_mb or limits_of(ctx)["memory_mb"],
        decoys=False,
        ledger_path=ctx.ledger_path,
        label=f"verify:{ctx.label}:{gate}",
    )
    return ctx.runner.open(spec)


def load_package(handle: SandboxHandle, blob: bytes) -> list[str]:
    """Unpack the frozen package into the sandbox and add the harness and the SDK. Returns the package's file names."""
    packed, names = repack_blob(blob)
    handle.thaw(packed)
    handle.put(harness.HARNESS_PATH, harness.HARNESS_SOURCE.encode("utf-8"))
    handle.put(harness.SDK_PATH, sdk_source())
    return names


@dataclass
class HarnessRun:
    """One call of the harness. `report` is the dict in the envelope's result when `kind == "ok"`."""

    kind: str  # ok, timeout, killed (the process was killed from outside), noresult (nothing usable was printed)
    report: dict[str, Any] = field(default_factory=dict)
    stderr_tail: str = ""
    exit_code: int = 0

    @property
    def usable(self) -> bool:
        return self.kind == "ok"


def run_harness(
    handle: SandboxHandle, flags: list[str], request: dict[str, Any], *, timeout_s: int, max_out: int
) -> HarnessRun:
    """Write the request, run `harness.py <nonce> <flags>`, and read the envelope. A dead sandbox raises `SandboxError`."""
    nonce = secrets.token_hex(16)
    handle.put(harness.REQUEST_PATH, json.dumps(request).encode("utf-8"))
    output = handle.run(
        ["python", harness.HARNESS_PATH, nonce, *flags],
        timeout_s,
        min(max_out + ENVELOPE_OVERHEAD, MAX_OUT_BYTES),
    )
    stderr = clip(output.stderr.decode("utf-8", "replace")[-harness.TAIL_CHARS :], harness.TAIL_CHARS)
    if output.timed_out:
        return HarnessRun("timeout", stderr_tail=stderr, exit_code=output.exit_code)
    envelope = parse_envelope(output.stdout, nonce)
    if envelope is not None and envelope.ok and isinstance(envelope.result, dict):
        return HarnessRun("ok", envelope.result, stderr, output.exit_code)
    if envelope is None and (output.exit_code < 0 or output.exit_code >= 128):
        return HarnessRun("killed", stderr_tail=stderr, exit_code=output.exit_code)
    return HarnessRun("noresult", stderr_tail=stderr, exit_code=output.exit_code)


def describe_failed_run(run: HarnessRun, what: str) -> str:
    if run.kind == "timeout":
        return f"{what} did not finish in time and the sandbox was stopped"
    if run.kind == "killed":
        return f"{what} was killed from outside (exit code {run.exit_code}); it probably used too much memory"
    return f"{what} produced no usable report (exit code {run.exit_code})"


# --- Reading reports, defensively ------------------------------------------------------------------------------------------


def count(value: Any, ceiling: int = 1_000_000) -> int:
    """A non-negative integer from untrusted JSON, or 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, ceiling))


def text(value: Any, limit: int = _ERROR_CHARS) -> str:
    return clip(value if isinstance(value, str) else "", limit)


@dataclass(frozen=True)
class Listed:
    id: str
    error: str


@dataclass(frozen=True)
class TestReport:
    """The `--tests` report, after checking. Every count is an int, every id and error is short clean text."""

    __test__ = False  # not a pytest class, whatever its name suggests

    stub: bool
    files: int
    ran: int
    passed: int
    skipped: int
    failed_count: int
    error_count: int
    not_run: int
    failed: tuple[Listed, ...]
    errors: tuple[Listed, ...]

    @property
    def clean(self) -> bool:
        return self.failed_count == 0 and self.error_count == 0 and self.not_run == 0

    @property
    def first_problem(self) -> Listed | None:
        return (self.failed or self.errors or (None,))[0]


def _listed(items: Any) -> tuple[Listed, ...]:
    if not isinstance(items, list):
        return ()
    return tuple(
        Listed(text(item.get("id"), _ID_CHARS), text(item.get("error")))
        for item in items[:MAX_LISTED]
        if isinstance(item, dict)
    )


def read_test_report(report: dict[str, Any]) -> TestReport | None:
    """The report, or None when it is not one (a missing count, a wrong type): the gate then fails instead of guessing."""
    required = ("ran", "passed", "skipped", "failed_count", "error_count", "not_run")
    if any(isinstance(report.get(key), bool) or not isinstance(report.get(key), int) for key in required):
        return None
    return TestReport(
        stub=report.get("stub") is True,
        files=count(report.get("files")),
        ran=count(report["ran"]),
        passed=count(report["passed"]),
        skipped=count(report["skipped"]),
        failed_count=count(report["failed_count"]),
        error_count=count(report["error_count"]),
        not_run=count(report["not_run"]),
        failed=_listed(report.get("failed")),
        errors=_listed(report.get("errors")),
    )


@dataclass(frozen=True)
class ItemOutcome:
    """One item of a `--batch` report. `error_type` is empty when the call succeeded."""

    index: int
    ok: bool
    error_type: str
    message: str
    result: Any
    result_bytes: int
    duration_ms: int


BATCH_ERROR_TYPES = (*harness.ENVELOPE_ERROR_TYPES, "Timeout")


def read_batch_report(report: dict[str, Any], expected: int) -> tuple[list[ItemOutcome], int] | None:
    """`(outcomes, not_run)`; None when the report is malformed or does not account for exactly `expected` items."""
    items, not_run = report.get("items"), report.get("not_run")
    if not isinstance(items, list) or isinstance(not_run, bool) or not isinstance(not_run, int):
        return None
    if not 0 <= not_run <= expected or len(items) + not_run != expected:
        return None
    outcomes: list[ItemOutcome] = []
    for position, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or item.get("index") != position
            or not isinstance(item.get("ok"), bool)
        ):
            return None
        error = item.get("error")
        if item["ok"] != (error is None):
            return None
        kind = ""
        message = ""
        if isinstance(error, dict):
            raw = error.get("type")
            kind = raw if raw in BATCH_ERROR_TYPES else "Crash"
            message = text(error.get("message"))
        elif error is not None:
            return None
        outcomes.append(
            ItemOutcome(
                position,
                item["ok"],
                kind,
                message,
                item.get("result"),
                count(item.get("result_bytes"), 1 << 40),
                count(item.get("duration_ms"), 1 << 31),
            )
        )
    return outcomes, not_run


__all__ = [
    "BATCH_ERROR_TYPES",
    "GRACE_S",
    "MAX_LISTED",
    "HarnessRun",
    "ItemOutcome",
    "Listed",
    "TestReport",
    "count",
    "describe_failed_run",
    "limits_of",
    "load_package",
    "open_sandbox",
    "read_batch_report",
    "read_test_report",
    "run_harness",
    "text",
]
