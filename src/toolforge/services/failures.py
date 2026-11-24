"""What ToolForge does when a call fails: remember its shape, optionally remember its input, and pull a tool that keeps failing (P12).

Three small, separate jobs, called by `ToolRunner` after a call has been recorded:

1. **Shape fingerprint** (`fingerprint`). Types and sizes of the arguments, never values: a string is `{"t": "str", "len": 120}`, a number
   is its kind, its sign and its digit count, a list is its length and the kinds of its items, an object is its key count. Only top-level
   keys that the manifest's own `input_schema` declares are named, and only when they are plain identifiers; every other key is counted,
   not named, because a key is caller text too. Files are a count and a total size, never a name. Proven by
   `tests/test_failures.py::test_a_fingerprint_contains_no_value_and_no_undeclared_key`.
2. **Raw input** (`stored_input`). The arguments themselves are kept only when `store_failed_inputs` is true, no file was attached, the
   arguments are under 8 KB as JSON and `secretscan` finds nothing secret-shaped in them. Otherwise the failure is recorded with its
   shape alone, and its hash is not recorded either (a hash of a short input can be guessed). What a stored input exposes is stated in
   `docs/repair.md`.
3. **Automatic quarantine** (`quarantine_verdict`). A failure rate above 40 percent over at least 10 of the last 20 calls pulls the active
   version. A failure is `Crash`, `Timeout`, `SandboxCrashed` or `NoResult`. A handled `ToolError` is the tool doing its job for a caller
   who sent something it refuses, so it is left out of the window altogether (neither a failure nor a good call), and a call rejected by
   schema validation never reaches the statistics. `OutputTooLarge` and `NotJson` count against a tool's success rate but not toward
   quarantine: the guide lists four failure classes and this module does exactly those. A `PolicyViolation` is handled by the runner at
   once, without a window. Only measured outcomes count: a problem report from an agent never gets here (`tests/test_problem_reports.py`).

Quarantine only ever takes a version out of use. Putting it back (`release`) or going back to an older one (`rollback`) is the operator's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from toolforge.config import Settings
from toolforge.verify import secretscan

log = logging.getLogger(__name__)

# A call that ends in one of these is captured (error class and shape; the input only if the settings allow it).
CAPTURED = ("Crash", "Timeout", "SandboxCrashed", "NoResult", "PolicyViolation")
# These four count toward automatic quarantine.
QUARANTINE_FAILURES = ("Crash", "Timeout", "SandboxCrashed", "NoResult")
CALLER_ERRORS = ("ToolError",)
QUARANTINE_WINDOW = 20
QUARANTINE_MIN_CALLS = 10
QUARANTINE_RATE = 0.40  # strictly above this
MAX_STORED_INPUT_BYTES = 8 * 1024
MAX_SHAPE_DEPTH = 4
MAX_SHAPE_ITEM_KINDS = 5
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


# --- Shape fingerprint --------------------------------------------------------------------------------------------------


def _sign(number: float) -> str:
    return "neg" if number < 0 else "zero" if number == 0 else "pos"


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "other"


def shape_of(value: Any, declared: Collection[str] = (), *, _depth: int = 0) -> dict[str, Any]:
    """The shape of a JSON value: its type and sizes, never what it says. `declared` names the top-level keys that may be named."""
    kind = _kind(value)
    if _depth >= MAX_SHAPE_DEPTH:
        return {"t": kind, "deep": True}
    if kind == "str":
        return {"t": "str", "len": len(value)}
    if kind == "int":
        return {"t": "int", "sign": _sign(value), "digits": len(str(abs(value)))}
    if kind == "float":
        return {"t": "float", "sign": _sign(value)}
    if kind == "array":
        kinds = sorted({_kind(item) for item in value})
        return {"t": "array", "len": len(value), "items": kinds[:MAX_SHAPE_ITEM_KINDS]}
    if kind == "object":
        named = {
            key: shape_of(item, (), _depth=_depth + 1)
            for key, item in value.items()
            if _depth == 0 and isinstance(key, str) and key in declared and _NAME.fullmatch(key)
        }
        out: dict[str, Any] = {"t": "object", "keys": len(value)}
        if named:
            out["props"] = dict(sorted(named.items()))
        if len(named) < len(value):
            out["unnamed_keys"] = len(value) - len(named)
        return out
    return {"t": kind}


def declared_keys(manifest: Mapping[str, Any]) -> list[str]:
    """The property names of the manifest's input schema: the only keys a fingerprint may name."""
    schema = manifest.get("input_schema") if isinstance(manifest, Mapping) else None
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    return [key for key in props if isinstance(key, str)] if isinstance(props, Mapping) else []


def fingerprint(
    args: Any, files: Mapping[str, bytes] | None = None, declared: Collection[str] = ()
) -> dict[str, Any]:
    """The shape fingerprint of one call's input: `{"args": <shape>, "files": {"count", "bytes"}}`."""
    attached = files or {}
    return {
        "args": shape_of(args, declared),
        "files": {"count": len(attached), "bytes": sum(len(data) for data in attached.values())},
    }


# --- Raw input ----------------------------------------------------------------------------------------------------------


def stored_input(settings: Settings, args: Any, files: Mapping[str, bytes] | None) -> tuple[str | None, str]:
    """`(input_json, sha256)` when the arguments may be kept as a regression case, else `(None, "")`. See the module docstring."""
    if not settings.store_failed_inputs or files or not isinstance(args, dict):
        return None, ""
    try:
        text = json.dumps(args, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None, ""
    if len(text.encode("utf-8")) >= MAX_STORED_INPUT_BYTES:
        return None, ""
    if secretscan.scan(text, "arguments"):
        return None, ""
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Automatic quarantine -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Why a version is pulled: `failures` of `calls` (ToolErrors left out) in the window, a rate above the limit."""

    failures: int
    calls: int

    @property
    def rate(self) -> float:
        return self.failures / self.calls

    def reason(self, call_id: str) -> str:
        return (
            f"{self.failures} of the last {self.calls} calls failed ({self.rate:.0%}), over the {QUARANTINE_RATE:.0%} limit "
            f"(measured after call {call_id})"
        )


def quarantine_verdict(outcomes: list[tuple[bool, str | None]]) -> Verdict | None:
    """The verdict for a version's recent calls, newest first as `(ok, error_class)`, or None when it may stay.

    Looks at the last `QUARANTINE_WINDOW` calls, leaves out the handled `ToolError`s, and needs at least `QUARANTINE_MIN_CALLS` of
    what remains. The rate must be strictly above `QUARANTINE_RATE`: 4 of 10 is 40 percent and stays, 5 of 10 goes.
    """
    window = [(ok, kind) for ok, kind in outcomes[:QUARANTINE_WINDOW] if kind not in CALLER_ERRORS]
    if len(window) < QUARANTINE_MIN_CALLS:
        return None
    failures = sum(1 for ok, kind in window if not ok and kind in QUARANTINE_FAILURES)
    verdict = Verdict(failures, len(window))
    return verdict if verdict.rate > QUARANTINE_RATE else None


# --- Used by the runner -------------------------------------------------------------------------------------------------


def capture(
    registry: Any,
    settings: Settings,
    *,
    version_id: int,
    manifest: Mapping[str, Any],
    error_class: str,
    args: Any,
    files: Mapping[str, bytes] | None,
) -> bool:
    """Record a failed call's shape (and, if allowed, its input). False when the registry has no failure storage (a test stand-in)."""
    store = getattr(registry, "record_failure", None)
    if store is None:
        return False
    raw, sha = stored_input(settings, args, files)
    store(
        version_id,
        error_class,
        fingerprint(args, files, declared_keys(manifest)),
        input_json=raw,
        input_sha256=sha,
    )
    return True


def judge(registry: Any, version_id: int) -> Verdict | None:
    """The quarantine verdict from the registry's recent calls of this version; None when there is none or no way to look."""
    look = getattr(registry, "recent_outcomes", None)
    if look is None:
        return None
    return quarantine_verdict(look(version_id, QUARANTINE_WINDOW))


__all__ = [
    "CALLER_ERRORS",
    "CAPTURED",
    "MAX_STORED_INPUT_BYTES",
    "QUARANTINE_FAILURES",
    "QUARANTINE_MIN_CALLS",
    "QUARANTINE_RATE",
    "QUARANTINE_WINDOW",
    "Verdict",
    "capture",
    "declared_keys",
    "fingerprint",
    "judge",
    "quarantine_verdict",
    "shape_of",
    "stored_input",
]
