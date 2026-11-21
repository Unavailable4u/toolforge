"""The Blast Radius report of one tool version: what it did while it ran, measured from hash-chained ledgers, never claimed.

Built on MiniLocker's `build_report` (and its HTML renderer), which reads the events a sandbox and its `Watch` wrote. ToolForge adds
five rules, because that builder was written for an agent's task and a tool version is not one:

* **Scope.** The verification ledger holds every gate's sandboxes. The report looks only at the containment run, from the start of
  gate G6 on, so the build gate's package downloads cannot show up as "network" and the build sandbox cannot hide the containment
  run's numbers. The whole file is still checked for tampering (`ledger_verified`), not just the part that is read.
* **Complete means destroyed.** MiniLocker marks a run complete when it finds a `task.end` event, which ToolForge ledgers never hold.
  A run counts as finished when every sandbox it created has a matching `sandbox.destroyed` event; only then does `in_progress` become
  `measured`.
* **No zero for a gap.** MiniLocker counts decoy reads as an empty list when no decoy was planted, and reports `network` as measured
  with zero attempts when no proxy existed. Here the decoy numbers say "not measured" when nothing was planted, and `network` says
  `no_route` only when Docker itself reported `network_mode: none` for every sandbox.
* **Only what applies.** MiniLocker's policy, time, browser and artifact dimensions describe an agent's shell and browser, not a tool
  call; they are left out and listed under `not_applicable` instead of being shown as zeros.
* **Hostile text.** File names, paths and anything else that came from the tool are cleaned (`textsafety.sanitize`) before they are
  stored, printed or turned into HTML.

The report is a derived copy. The ledgers are the truth, `generate` can rebuild it at any time, and it is stored next to them as
`blast-radius.json`. A passing report is evidence, not proof (the footer says so): decoys are found by access time and by values that
reach output or files, and a tool that only reads an environment variable and tells nobody is not seen.

MiniLocker is only reached through `_ml_path.load`, never imported at module level. Proven by `tests/test_blast_radius.py`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from toolforge import ledger as ledger_io
from toolforge import textsafety
from toolforge.sandbox import _ml_path

if TYPE_CHECKING:
    from toolforge.config import Settings

log = logging.getLogger(__name__)

REPORT_FILE = "blast-radius.json"
SCHEMA = 1
MAX_SAMPLED_CALLS = 20  # the most recent real calls whose ledgers are read
MAX_STORED_BYTES = 1024 * 1024
TEXT_CHARS = 300
LIST_CAP = 30
NOT_MEASURED = "not measured"
CONTAINMENT_GATE = "G6"

DIMENSIONS = ("network", "filesystem", "secrets", "compute", "persistence")
NOT_APPLICABLE = {
    "policy": "MiniLocker's shell policy scores an agent's commands; a tool call has none.",
    "time": "MiniLocker's wall clock needs a task start and end; ToolForge records durations per gate and per call instead.",
    "browser": "tools do not drive a browser in this release.",
    "artifacts": "a tool returns a result, not stored artifacts.",
}
FOOTER = (
    "A clean report is evidence, not proof. It says what was observed in the runs listed here, inside the sandbox. Decoys are noticed "
    "by access time and by values that reach output or files; reading an environment variable and telling nobody is not seen. "
    "'not measured' means nothing was observed and no claim is made. The container is the security boundary."
)

# Verdict statuses, strongest first.
LEDGER_BROKEN, VIOLATION_SEEN, NOT_MEASURED_STATUS = "ledger_broken", "violation_seen", "not_measured"
INCOMPLETE, NO_VIOLATION_SEEN = "incomplete", "no_violation_seen"
VERDICT_TEXT = {
    LEDGER_BROKEN: "A ledger did not verify or could not be read, so nothing in this report can be trusted.",
    VIOLATION_SEEN: "Something was seen that a tool should never do.",
    NOT_MEASURED_STATUS: "Nothing was measured for this version.",
    INCOMPLETE: "No violation was seen in what was measured, but not everything was measured.",
    NO_VIOLATION_SEEN: "No violation was seen in what was measured. This is evidence, not proof.",
}
_DECOY_LISTS = ("decoys_read", "decoys_exposed_to_model", "decoys_copied_out", "decoys_tripped")
_DECOY_COUNTS = ("exfiltration_attempts", "exfiltration_blocked")


# --- Small helpers -------------------------------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clean(value: Any, *, depth: int = 0) -> Any:
    """`value` with every string made safe to show (no control or invisible characters, clipped) and every list capped."""
    if isinstance(value, str):
        return textsafety.sanitize(value, max_chars=TEXT_CHARS)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if depth > 6:
        return NOT_MEASURED
    if isinstance(value, dict):
        return {
            textsafety.sanitize(str(k), max_chars=80): clean(v, depth=depth + 1)
            for k, v in list(value.items())[:80]
        }
    if isinstance(value, (list, tuple)):
        return [clean(v, depth=depth + 1) for v in list(value)[:LIST_CAP]]
    return textsafety.sanitize(str(value), max_chars=TEXT_CHARS)


def _ml_report() -> Any:
    return _ml_path.load("minilocker.ledger.report")


@dataclass
class LedgerSource:
    """One ledger file's events, or what went wrong reading it (`problem`). `name` is a file name, never a full path."""

    name: str
    events: list[dict[str, Any]] = field(default_factory=list)
    problem: str | None = None


@dataclass
class _Analysis:
    name: str
    dims: dict[str, dict[str, Any]]
    finished: bool
    verified: bool
    detail: str
    head_hash: str | None
    events: int
    window_events: int


def slice_from_gate(events: Sequence[dict[str, Any]], gate: str = CONTAINMENT_GATE) -> list[dict[str, Any]]:
    """The events from the start of the last run of `gate` on; all of them when the ledger holds no such start (a dry run)."""
    start = 0
    for index, event in enumerate(events):
        if event.get("type") == "forge.gate.started" and (event.get("payload") or {}).get("gate") == gate:
            start = index
    return list(events[start:])


def _finished(events: Iterable[dict[str, Any]]) -> bool:
    created: set[Any] = set()
    destroyed: set[Any] = set()
    for event in events:
        name = (event.get("payload") or {}).get("name")
        if event.get("type") in ("sandbox.created", "sandbox.recreated"):
            created.add(name)
        elif event.get("type") == "sandbox.destroyed":
            destroyed.add(name)
    return bool(created) and created <= destroyed


def _settle(dim: dict[str, Any], finished: bool) -> dict[str, Any]:
    """MiniLocker says `in_progress` until a task ends; for a tool run that is when every sandbox is gone."""
    if dim.get("status") == "in_progress" and finished:
        return {**dim, "status": "measured"}
    return dim


def _network(window: Sequence[dict[str, Any]]) -> dict[str, Any]:
    created = [e for e in window if e.get("type") == "sandbox.created"]
    egress = [e for e in window if e.get("type") in ("egress.allowed", "egress.blocked")]
    modes = [((e.get("payload") or {}).get("hardening") or {}).get("network_mode") for e in created]
    if egress:
        hosts = {(e.get("payload") or {}).get("host") for e in egress} - {None}
        allowed = sum(1 for e in egress if e.get("type") == "egress.allowed")
        return {
            "status": "measured",
            "attempts": len(egress),
            "allowed": allowed,
            "blocked": len(egress) - allowed,
            "hosts": sorted(str(h) for h in hosts)[:LIST_CAP],
            "evidence": [e.get("id") for e in egress][:LIST_CAP],
        }
    if created and all(mode == "none" for mode in modes):
        return {
            "status": "no_route",
            "network_mode": "none",
            "sandboxes": len(created),
            "note": "Docker reported network_mode none for every sandbox: there was no route out, so nothing could be attempted "
            "and nothing was observed.",
        }
    return {
        "status": "not_measured",
        "note": "the ledger holds no egress events and no sandbox that Docker confirmed had no network",
    }


def _secrets(dim: dict[str, Any]) -> dict[str, Any]:
    if dim.get("status") == "not_measured" or dim.get("decoys_planted"):
        return dim
    out = dict(dim)
    for key in (*_DECOY_LISTS, *_DECOY_COUNTS):
        if key in out:
            out[key] = NOT_MEASURED
    out["decoys_note"] = "no decoy was planted in this run, so what the tool did with decoys was not measured"
    return out


def _analyse(source: LedgerSource, task_id: str, *, from_gate: str | None) -> _Analysis:
    """Verify the whole file, then measure the part that matters."""
    verified, detail = ledger_io.verify_chain(source.events)
    if source.problem:
        verified, detail = False, source.problem
    window = slice_from_gate(source.events, from_gate) if from_gate else list(source.events)
    built = _ml_report().build_report(task_id, window)
    finished = _finished(window)
    ml = built["dimensions"]
    dims: dict[str, dict[str, Any]] = {
        "network": _network(window),
        "filesystem": _settle(ml["filesystem"], finished),
        "secrets": _secrets(_settle(ml["secrets"], finished)),
        "compute": _settle(ml["compute"], finished),
        "persistence": _settle(ml["persistence"], finished),
    }
    return _Analysis(
        name=source.name,
        dims=dims,
        finished=finished,
        verified=verified,
        detail=detail,
        head_hash=source.events[-1].get("hash") if source.events else None,
        events=len(source.events),
        window_events=len(window),
    )


def _touched(dims: dict[str, dict[str, Any]]) -> list[str]:
    """Decoy item ids that were read, shown to the model, copied out or used, across one analysis."""
    secrets = dims["secrets"]
    found: set[str] = set()
    for key in _DECOY_LISTS:
        value = secrets.get(key)
        if isinstance(value, list):
            found.update(str(v) for v in value)
    return sorted(found)


def _measured(dim: dict[str, Any]) -> bool:
    return dim.get("status") not in (None, "not_measured", "not_used")


# --- The runtime sample ---------------------------------------------------------------------------------------------------


def _runtime(analyses: Sequence[_Analysis], found: int) -> dict[str, Any]:
    if not analyses:
        return {
            "status": "not_measured",
            "note": "no real call is recorded for this version, so nothing was observed at run time",
        }
    planted = [a for a in analyses if a.dims["secrets"].get("decoys_planted")]
    touched = [a for a in analyses if _touched(a.dims)]
    filesystem = [a.dims["filesystem"] for a in analyses if _measured(a.dims["filesystem"])]
    compute = [a.dims["compute"] for a in analyses if _measured(a.dims["compute"])]
    return {
        "status": "measured",
        "calls_sampled": len(analyses),
        "calls_found": found,
        "calls_with_decoys_planted": len(planted),
        "decoy_watch": "complete"
        if len(planted) == len(analyses)
        else "partial"
        if planted
        else "not_measured",
        "calls_with_decoy_events": len(touched),
        "decoys_touched": sorted({item for a in touched for item in _touched(a.dims)}),
        "rootfs_changes": sum(int(f.get("rootfs_changes") or 0) for f in filesystem)
        if filesystem
        else NOT_MEASURED,
        "peak_memory_bytes": max(
            (int(c.get("peak_memory_bytes") or 0) for c in compute), default=NOT_MEASURED
        ),
        "peak_pids": max((int(c.get("peak_pids") or 0) for c in compute), default=NOT_MEASURED),
        "calls_not_audited": sum(
            1 for a in analyses if not (_measured(a.dims["filesystem"]) and _measured(a.dims["compute"]))
        ),
        "ledgers_verified": all(a.verified for a in analyses),
    }


# --- The verdict -----------------------------------------------------------------------------------------------------------


def _violations(dims: dict[str, dict[str, Any]], where: str) -> list[str]:
    reasons: list[str] = []
    touched = _touched(dims)
    if touched:
        reasons.append(f"{where}: a planted decoy was touched ({', '.join(touched)})")
    secrets = dims["secrets"]
    if secrets.get("real_secrets_found"):
        reasons.append(f"{where}: a real secret was found inside the sandbox")
    filesystem = dims["filesystem"]
    if filesystem.get("rootfs_changes"):
        reasons.append(f"{where}: the root filesystem changed ({filesystem['rootfs_changes']} changes)")
    if filesystem.get("host_binds"):
        reasons.append(f"{where}: a host folder was mounted into the sandbox")
    if dims["compute"].get("oom_killed") is True:
        reasons.append(f"{where}: the sandbox was killed for using too much memory")
    network = dims["network"]
    if network.get("status") == "measured" and network.get("attempts"):
        reasons.append(f"{where}: the sandbox tried to reach the network ({network['attempts']} attempts)")
    return reasons


def _gaps(analysis: _Analysis) -> list[str]:
    gaps: list[str] = []
    dims = analysis.dims
    for name in ("network", "filesystem", "secrets", "compute"):
        if not _measured(dims[name]):
            gaps.append(f"{name} was not measured")
    if _measured(dims["secrets"]) and not dims["secrets"].get("decoys_planted"):
        gaps.append("no decoy was planted, so decoy use was not measured")
    if not analysis.finished:
        gaps.append("the run had not finished (a sandbox was not destroyed)")
    return gaps


def _verdict(
    verification: _Analysis | None, calls: Sequence[_Analysis], runtime: dict[str, Any]
) -> dict[str, Any]:
    violations: list[str] = []
    gaps: list[str] = []
    verified = verification is not None and verification.verified and all(a.verified for a in calls)
    if verification is None:
        gaps.append("no verification ledger was found for this version")
    else:
        violations += _violations(verification.dims, "verification run")
        gaps += _gaps(verification)
    for analysis in calls:
        violations += _violations(analysis.dims, f"call ledger {analysis.name}")
    if runtime.get("status") == "not_measured":
        gaps.append("no real call has been sampled yet")
    elif runtime.get("decoy_watch") != "complete":
        gaps.append("some sampled calls ran without decoys, so decoy use there was not measured")
    if verification is None and not calls:
        status = NOT_MEASURED_STATUS
    elif not verified:
        status = LEDGER_BROKEN
    elif violations:
        status = VIOLATION_SEEN
    elif verification is not None and all(
        not _measured(verification.dims[name]) for name in ("network", "filesystem", "secrets", "compute")
    ):
        status = NOT_MEASURED_STATUS
    elif gaps:
        status = INCOMPLETE
    else:
        status = NO_VIOLATION_SEEN
    return {
        "status": status,
        "summary": VERDICT_TEXT[status],
        "violations": violations[:LIST_CAP],
        "not_measured": gaps[:LIST_CAP],
    }


# --- Building ---------------------------------------------------------------------------------------------------------------


def build(
    tool: str,
    version: str,
    *,
    verification: LedgerSource | None,
    calls: Sequence[LedgerSource] = (),
    calls_found: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """The report document for `tool@version` from ledger events already read. Pure: nothing is read or written here.

    `verification` is the verification ledger (the part from G6 on is measured); `calls` are ledgers of sampled real calls and
    `calls_found` how many existed (more than were sampled). Needs MiniLocker's report builder, so it raises `SandboxUnavailable`
    when that cannot be imported.
    """
    label = f"{tool}@{version}"
    ver = (
        _analyse(verification, f"verify:{label}", from_gate=CONTAINMENT_GATE)
        if verification is not None
        else None
    )
    analyses = [_analyse(call, f"call:{label}:{call.name}", from_gate=None) for call in calls]
    runtime = _runtime(analyses, calls_found if calls_found is not None else len(analyses))
    if ver is not None:
        dimensions = {name: ver.dims[name] for name in DIMENSIONS}
        scope = "the containment run (gate G6) of the verification ledger"
    else:
        dimensions = {
            name: {"status": "not_measured", "note": "no verification ledger"} for name in DIMENSIONS
        }
        scope = "none"
    sources: dict[str, Any] = {
        "verification": {
            "scope": scope,
            "ledger": verification.name if verification is not None else None,
            "events": ver.events if ver else 0,
            "events_measured": ver.window_events if ver else 0,
            "verified": ver.verified if ver else None,
            "detail": ver.detail if ver else "no verification ledger was found",
            "head_hash": ver.head_hash if ver else None,
        },
        "calls": [
            {"ledger": a.name, "events": a.events, "verified": a.verified, "head_hash": a.head_hash}
            for a in analyses
        ],
    }
    document = {
        "schema": SCHEMA,
        "tool": tool,
        "version": version,
        "generated_at": now or _now(),
        "dimensions": dimensions,
        "runtime_sample": runtime,
        "ledger_verified": bool(ver is not None and ver.verified and all(a.verified for a in analyses)),
        "verdict": _verdict(ver, analyses, runtime),
        "sources": sources,
        "not_applicable": dict(NOT_APPLICABLE),
        "footer": FOOTER,
    }
    cleaned: dict[str, Any] = clean(document)
    return cleaned


# --- Reading ledgers from disk, storing and loading the report -------------------------------------------------------------------


def _newest(paths: Iterable[Path]) -> list[Path]:
    def age(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(paths, key=age, reverse=True)


def _read_source(path: Path) -> LedgerSource:
    events, problem = ledger_io.read_ledger(path)
    return LedgerSource(path.name, events, problem)


def report_path(settings: Settings, label: str) -> Path:
    return ledger_io.version_ledger_dir(settings.home, label) / REPORT_FILE


def collect_sources(
    settings: Settings, label: str, verification_path: Path | None = None
) -> tuple[LedgerSource | None, list[LedgerSource], int]:
    """`(verification ledger, sampled call ledgers newest first, number of call ledgers that exist)`.

    The verification ledger is `verification_path` when given, else the newest `verify-*.jsonl` of the version.
    """
    folder = ledger_io.version_ledger_dir(settings.home, label)
    try:
        files = [p for p in folder.glob("*.jsonl") if p.is_file()]
    except OSError:
        files = []
    verify_files = _newest(p for p in files if p.name.startswith("verify-"))
    call_files = _newest(
        p for p in files if not p.name.startswith("verify-") and p.name != ledger_io.OPERATOR_LEDGER
    )
    chosen = (
        verification_path if verification_path is not None else (verify_files[0] if verify_files else None)
    )
    verification = _read_source(chosen) if chosen is not None else None
    sample = [_read_source(p) for p in call_files[:MAX_SAMPLED_CALLS]]
    return verification, sample, len(call_files)


def generate(
    settings: Settings,
    name: str,
    version: str,
    *,
    verification_path: Path | None = None,
    store: bool = True,
) -> dict[str, Any]:
    """Build the report for a version from the ledgers on disk and, with `store`, write it next to them.

    `verification_path` names the verification ledger to measure (the pipeline passes the run it just finished); without it the
    newest one is used. Real calls are read from the version's other ledgers, newest `MAX_SAMPLED_CALLS`.
    """
    label = f"{name}@{version}"
    verification, sample, found = collect_sources(settings, label, verification_path)
    document = build(name, version, verification=verification, calls=sample, calls_found=found)
    if store:
        write(settings, label, document)
    return document


def write(settings: Settings, label: str, document: dict[str, Any]) -> Path:
    """Store `document` as `blast-radius.json`, replacing an earlier copy in one step so a reader never sees half a file."""
    path = report_path(settings, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".blast-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(document, out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return path


def load_stored(settings: Settings, label: str) -> tuple[dict[str, Any] | None, str | None]:
    """`(document, problem)`. `(None, None)` means no report was stored; a problem says why a stored one could not be used."""
    path = report_path(settings, label)
    try:
        if path.stat().st_size > MAX_STORED_BYTES:
            return None, "the stored report is larger than 1 MB"
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return None, "the stored report could not be read"
    if (
        not isinstance(data, dict)
        or data.get("schema") != SCHEMA
        or not isinstance(data.get("dimensions"), dict)
    ):
        return None, "the stored report has an unknown format"
    cleaned: dict[str, Any] = clean(data)
    return cleaned, None


# --- Showing it ---------------------------------------------------------------------------------------------------------------


def _flat(dimension: dict[str, Any]) -> dict[str, Any]:
    """A dimension reduced to values a model or a terminal can read: scalars, short lists, and workspace counts."""
    out: dict[str, Any] = {}
    for key, value in dimension.items():
        if key == "evidence":
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list) and all(isinstance(v, (str, int, float, bool)) for v in value):
            out[key] = value[:10]
        elif key == "workspace" and isinstance(value, dict):
            keep = ("files", "bytes", "added_count", "modified_count", "deleted_count")
            out[key] = {k: value[k] for k in keep if k in value}
        elif isinstance(value, dict):  # one level of small maps, such as `real_secrets_found`
            small = {
                str(k): (v[:10] if isinstance(v, list) else v)
                for k, v in list(value.items())[:10]
                if isinstance(v, (str, int, float, bool, type(None)))
                or (isinstance(v, list) and all(isinstance(i, (str, int, float, bool)) for i in v))
            }
            if small:
                out[key] = small
    return out


def summarize(document: dict[str, Any]) -> dict[str, Any]:
    """The short form that `forge_describe` and the release command show: verdict, dimensions by name, the runtime sample."""
    verdict = document.get("verdict") or {}
    dimensions = document.get("dimensions") or {}
    return {
        "status": verdict.get("status", NOT_MEASURED_STATUS),
        "summary": verdict.get("summary", VERDICT_TEXT[NOT_MEASURED_STATUS]),
        "violations": list(verdict.get("violations") or []),
        "not_measured": list(verdict.get("not_measured") or []),
        "generated_at": document.get("generated_at"),
        "ledger_verified": bool(document.get("ledger_verified")),
        "dimensions": {name: _flat(dim) for name, dim in dimensions.items() if isinstance(dim, dict)},
        "runtime_sample": _flat(document.get("runtime_sample") or {}),
        "footer": FOOTER,
    }


def missing_summary(problem: str | None = None) -> dict[str, Any]:
    """What to show for a version with no usable stored report: the gap, said as a gap."""
    return {
        "status": NOT_MEASURED_STATUS,
        "summary": problem or "no Blast Radius report has been stored for this version",
        "violations": [],
        "not_measured": ["everything"],
        "generated_at": None,
        "ledger_verified": False,
        "dimensions": {name: {"status": "not_measured"} for name in DIMENSIONS},
        "runtime_sample": {"status": "not_measured"},
        "footer": FOOTER,
    }


def render_text(document: dict[str, Any]) -> str:
    """A plain-text rendering for the terminal."""
    summary = summarize(document)
    lines = [
        f"Blast Radius: {document.get('tool')}@{document.get('version')}",
        f"verdict: {summary['status']} - {summary['summary']}",
        f"ledger chain verified: {'yes' if summary['ledger_verified'] else 'NO'}    generated: {summary['generated_at']}",
    ]
    lines += [f"  violation: {reason}" for reason in summary["violations"]]
    lines += [f"  not measured: {gap}" for gap in summary["not_measured"]]
    for name, values in summary["dimensions"].items():
        shown = ", ".join(f"{k}={v}" for k, v in values.items() if k not in ("note",))
        lines.append(f"{name}: {shown}")
        if values.get("note"):
            lines.append(f"    {values['note']}")
    runtime = ", ".join(f"{k}={v}" for k, v in summary["runtime_sample"].items() if k != "note")
    lines.append(f"runtime sample: {runtime}")
    if summary["runtime_sample"].get("note"):
        lines.append(f"    {summary['runtime_sample']['note']}")
    lines += ["", FOOTER]
    return "\n".join(textsafety.sanitize(line, max_chars=600) if line else "" for line in lines)


_ML_STEPS = " · n/a steps · n/a tool calls"


def render_html(document: dict[str, Any]) -> str:
    """The page, drawn by MiniLocker's own renderer (which escapes everything it prints) from a report in its shape."""
    verdict = document.get("verdict") or {}
    sources = document.get("sources") or {}
    verification = sources.get("verification") or {}
    dimensions: dict[str, Any] = {
        "verdict": {
            "status": verdict.get("status"),
            "violations": verdict.get("violations") or [],
            "not_measured": verdict.get("not_measured") or [],
        },  # fmt: skip
        **{name: dim for name, dim in (document.get("dimensions") or {}).items()},
        "runtime_sample": document.get("runtime_sample") or {},
        "not_applicable": document.get("not_applicable") or {},
    }
    shaped = {
        "task_id": f"{document.get('tool')}@{document.get('version')}",
        "task": f"What this tool version did while it ran in a sandbox. Scope: {verification.get('scope')}.",
        "outcome": verdict.get("status", NOT_MEASURED_STATUS),
        "summary": verdict.get("summary", ""),
        "complete": True,
        "steps": "n/a",
        "tool_calls": "n/a",
        "ledger": {
            "verified": bool(document.get("ledger_verified")),
            "detail": verification.get("detail") or "",
            "head_hash": verification.get("head_hash"),
        },
        "dimensions": dimensions,
        "note": document.get("footer") or FOOTER,
    }
    page: str = _ml_report().render_html(shaped)
    return page.replace(_ML_STEPS, "")


__all__ = [
    "DIMENSIONS",
    "FOOTER",
    "REPORT_FILE",
    "LedgerSource",
    "build",
    "clean",
    "collect_sources",
    "generate",
    "load_stored",
    "missing_summary",
    "render_html",
    "render_text",
    "report_path",
    "slice_from_gate",
    "summarize",
    "write",
]
