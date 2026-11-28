"""The sanitised recording of a demo run, the one thing the website's tour (P20) may show of it.

A recording is three files and nothing else:

* `recording.json`: date, ToolForge version, seed, whether the agent was replayed or live, which sandbox ran the tools, and counts.
* `events.json`: the run's ledger events, **allowlisted**: an event type must be in `KEPT_PREFIXES`, and only the payload keys in
  `FACTS` survive, each only if its value has the expected shape (an integer, a boolean, a short label, a gate id, or a hash cut to 12
  hex characters). Free text, source code, environment values, arguments and results never get in, whatever they say: a key that is
  not listed is dropped, and so is a value that does not fit its shape.
* `transcript.json`: the shape of each act's agent turns (tool, success, sizes, a hash of the arguments), never what the agent or a
  tool said.

`validate` re-checks a directory against the same rules and runs `verify.secretscan` over every file, so a committed recording that was
edited by hand, or made by an older version of this module, is caught by `tests/test_demo_recording.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = 1
FILES = ("recording.json", "events.json", "transcript.json")
KEPT_PREFIXES = ("forge.", "canary.", "egress.", "sandbox.created", "sandbox.destroyed", "sandbox.audit")
NATIVE_PREFIXES = ("canary.", "egress.", "sandbox.")  # MiniLocker's own events: the type only, no facts
MAX_EVENTS = 4000

LABEL = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
GATE = re.compile(r"^G[1-7]$")
SHORT_TEXT = re.compile(r"^[A-Za-z0-9 ().,_-]{1,60}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX12 = re.compile(r"^[0-9a-f]{12}$")

# payload key -> kind. Anything not listed is dropped.
FACTS: dict[str, str] = {
    "gate": "gate",
    "duration_ms": "int",
    "hints": "int",
    "from": "label",
    "to": "label",
    "ok": "bool",
    "error_type": "label",
    "result_bytes": "int",
    "args_bytes": "int",
    "file_bytes": "int",
    "decoy_events": "int",
    "egress_attempts": "int",
    "truncated": "bool",
    "sandbox_timed_out": "bool",
    "cleanup_ok": "bool",
    "quarantined": "bool",
    "real_secret_audit": "label",
    "state_bytes": "int",
    "snapshot_bytes": "int",
    "step": "int",
    "steps": "int",
    "from_step": "int",
    "to_step": "int",
    "submitted_by": "label",
    "resumed": "bool",
    "code_sha": "hash",
    "args_sha256": "hash",
    "state_sha256": "hash",
    "network": "count",
    "secrets": "count",
}
METRICS: dict[str, str] = {
    "failures": "int",
    "files": "int",
    "python_files": "int",
    "secret_findings": "int",
    "validation_errors": "int",
    "warnings": "int",
    "blob_bytes": "int",
    "installed_files": "int",
    "package_files": "int",
    "requirements": "int",
    "examples_checked": "int",
    "examples_failed": "int",
    "examples_live_skipped": "int",
    "tests_ran": "int",
    "tests_passed": "int",
    "tests_failed": "int",
    "tests_errors": "int",
    "tests_skipped": "int",
    "tests_failing_with_stub": "int",
    "inputs_planned": "int",
    "inputs_run": "int",
    "handled_errors": "int",
    "p95_ms": "int",
    "total_ms": "int",
    "cases_proposed": "int",
    "cases_usable": "int",
    "cases_executed": "int",
    "cases_failed": "int",
    "crashes": "int",
    "timeouts": "int",
    "decoy_events": "int",
    "decoys_planted": "int",
    "hardening_confirmed": "bool",
    "injection_scan": "label",
    "egress": "text",
    "model": "label",
    "tokens_in": "int",
    "tokens_out": "int",
}
PERMISSIONS = {"browser": "bool", "inputs": "bool", "network_hosts": "int", "secrets": "int"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) < 10**10


def shaped(kind: str, value: Any) -> Any:
    """The value cut to its shape, or `None` when it does not fit (a miss is dropped, never repaired)."""
    if kind == "int":
        return value if _is_int(value) else None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "label":
        return value if isinstance(value, str) and LABEL.match(value) else None
    if kind == "gate":
        return value if isinstance(value, str) and GATE.match(value) else None
    if kind == "text":
        return value if isinstance(value, str) and SHORT_TEXT.match(value) else None
    if kind == "hash":
        return value[:12] if isinstance(value, str) and HEX64.match(value) else None
    if kind == "count":  # a list of names becomes how many there are; the names are not kept
        if _is_int(value):
            return value
        return len(value) if isinstance(value, (list, tuple)) else None
    return None


def facts_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for key, kind in FACTS.items():
        if key in payload and (value := shaped(kind, payload[key])) is not None:
            facts[key] = value
    metrics = payload.get("metrics")
    if isinstance(metrics, Mapping):
        kept = {
            k: v
            for k, kind in METRICS.items()
            if k in metrics and (v := shaped(kind, metrics[k])) is not None
        }
        if kept:
            facts["metrics"] = kept
    permissions = payload.get("permissions")
    if isinstance(permissions, Mapping):
        kept = {
            k: v
            for k, kind in PERMISSIONS.items()
            if k in permissions and (v := shaped(kind, permissions[k])) is not None
        }
        if kept:
            facts["permissions"] = kept
    return facts


def kind_of_file(path: Path) -> str:
    if path.name == "operator.jsonl":
        return "operator"
    if path.name.startswith("verify-"):
        return "verify"
    if path.parent.name == "sessions":
        return "session"
    return "box"


def sanitise(
    files: Iterable[tuple[str, str, list[dict[str, Any]]]], windows: Mapping[str, tuple[float, float]]
) -> tuple[list[dict[str, Any]], int]:
    """`files` is `(tool label, ledger kind, events)`; `windows` maps an act id to the (start, end) epoch seconds it ran in.

    Returns the kept events in time order, with `t` in seconds from the first event, and how many events were dropped.
    """
    raw: list[tuple[float, dict[str, Any]]] = []
    dropped = 0
    for label, ledger, events in files:
        tool = label if LABEL.match(label) else ""
        for event in events:
            kind, ts = event.get("type"), event.get("ts")
            if not isinstance(kind, str) or not kind.startswith(KEPT_PREFIXES) or isinstance(ts, bool):
                dropped += 1
                continue
            if not isinstance(ts, (int, float)):
                dropped += 1
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            facts = {} if kind.startswith(NATIVE_PREFIXES) else facts_of(payload)
            if not LABEL.match(kind):
                dropped += 1
                continue
            item: dict[str, Any] = {"ledger": ledger, "tool": tool, "type": kind, "facts": facts}
            raw.append((float(ts), item))
    raw.sort(key=lambda pair: pair[0])
    if not raw:
        return [], dropped
    start = raw[0][0]
    out = []
    for ts, item in raw[:MAX_EVENTS]:
        # an event belongs to the last act that had started by then: acts run one after another, so their windows never overlap
        started = [(lo, a) for a, (lo, _) in windows.items() if lo <= ts]
        act = max(started)[1] if started else ""
        out.append({"t": round(ts - start, 3), "act": act, **item})
    return out, dropped + max(0, len(raw) - MAX_EVENTS)


def transcript_shape(acts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Per act: the agent's calls as an `AgentResult.record()` kept them (names, sizes, hashes) and how the act ended. No text."""
    out = []
    for act in acts:
        calls = []
        for call in act.get("calls", []):
            calls.append(
                {
                    "step": call["step"],
                    "tool": call["tool"] if LABEL.match(str(call["tool"])) else "unlisted",
                    "ok": bool(call["ok"]),
                    "duration_ms": call["duration_ms"],
                    "argument_chars": call["argument_chars"],
                    "result_chars": call["result_chars"],
                    "arguments_sha256": str(call["arguments_sha256"])[:12],
                }
            )
        out.append(
            {
                "act": act["act"],
                "title": act["title"],
                "task": act["task"],
                "sibling": bool(act["sibling"]),
                "stop_reason": act["stop_reason"],
                "answered": bool(act["answered"]),
                "checker": act["checker"],
                "steps": act["steps"],
                "requests": act["requests"],
                "tokens_estimated": act["tokens_estimated"],
                "tokens_measured": act["tokens_measured"],
                "wall_ms": act["wall_ms"],
                "turns_left_out": act.get("turns_left_out", 0),
                "calls": calls,
            }
        )
    return {"schema": SCHEMA, "acts": out}


def write_recording(
    directory: Path, meta: Mapping[str, Any], events: list[dict[str, Any]], transcript: Mapping[str, Any]
) -> list[Path]:
    """Write the three files. Refuses a directory that holds anything else, so a typo cannot overwrite somebody's work."""
    directory.mkdir(parents=True, exist_ok=True)
    stray = [p.name for p in directory.iterdir() if p.name not in FILES]
    if stray:
        raise FileExistsError(
            f"{directory} holds files that are not part of a recording ({', '.join(sorted(stray)[:5])}); use an empty directory"
        )
    bodies = {
        "recording.json": {"schema": SCHEMA, **meta},
        "events.json": {"schema": SCHEMA, "events": events},
        "transcript.json": transcript,
    }
    written = []
    for name in FILES:
        path = directory / name
        path.write_text(json.dumps(bodies[name], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def validate(directory: Path, environ: Mapping[str, str] | None = None) -> list[str]:
    """Everything wrong with a recording directory, as short sentences (an empty list means it is clean)."""
    from toolforge.verify import secretscan

    problems: list[str] = []
    present = sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []
    if present != sorted(FILES):
        return [f"a recording is exactly {', '.join(FILES)}; found {', '.join(present) or 'nothing'}"]
    docs: dict[str, Any] = {}
    for name in FILES:
        text = (directory / name).read_text(encoding="utf-8")
        try:
            docs[name] = json.loads(text)
        except ValueError:
            return [f"{name} is not JSON"]
        if docs[name].get("schema") != SCHEMA:
            problems.append(f"{name}: schema must be {SCHEMA}")
        for finding in secretscan.scan(text, name):
            problems.append(f"{name} line {finding.line}: a secret-shaped string ({finding.pattern})")
        for value in (environ or {}).values():
            if len(value) >= 8 and value in text:
                problems.append(f"{name} contains an environment value")
                break
    meta = docs["recording.json"]
    for key in ("recorded_at", "toolforge_version", "seed", "mode", "sandbox", "isolated"):
        if key not in meta:
            problems.append(f"recording.json lacks {key}")
    if meta.get("mode") not in ("replay", "live"):
        problems.append("recording.json: mode must be replay or live")
    allowed = {"t", "act", "ledger", "tool", "type", "facts"}
    for index, event in enumerate(docs["events.json"].get("events", [])):
        if set(event) - allowed:
            problems.append(f"event {index} has keys that are not allowed: {sorted(set(event) - allowed)}")
        if not str(event.get("type", "")).startswith(KEPT_PREFIXES):
            problems.append(f"event {index} has a type that is not kept: {event.get('type')!r}")
        facts = event.get("facts", {})
        for key, value in facts.items():
            if key not in FACTS and key not in ("metrics", "permissions"):
                problems.append(f"event {index} has a fact that is not allowed: {key}")
            elif key in ("metrics", "permissions"):
                table = METRICS if key == "metrics" else PERMISSIONS
                for inner, inner_value in value.items():
                    if inner not in table or shaped(table[inner], inner_value) is None:
                        problems.append(f"event {index} has a {key} value that is not allowed: {inner}")
            elif FACTS[key] == "hash":
                if not (isinstance(value, str) and HEX12.match(value)):
                    problems.append(f"event {index}: {key} must be 12 hex characters")
            elif shaped(FACTS[key], value) is None:
                problems.append(f"event {index}: {key} does not have the shape it should")
        for text in _walk_strings(facts):
            if "\n" in text or len(text) > 64:
                problems.append(f"event {index} holds a long or multi-line string")
                break
    return problems
