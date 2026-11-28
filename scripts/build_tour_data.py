"""Turn one recorded demo run into `site/data/tour.json`, the data the website's guided tour plays (P20).

    python scripts/build_tour_data.py                                  # demo/recordings/main -> site/data/tour.json
    python scripts/build_tour_data.py demo/recordings/main             # the same, spelled out
    python scripts/build_tour_data.py --illustrative                   # the invented sample story, never a recording
    python scripts/build_tour_data.py demo/recordings/x --out /tmp/tour.json

A recording is the three files `demo/recording.py` (P18) writes: `recording.json`, `events.json` and `transcript.json`. This script reads them
**as untrusted input**, even though P18 already sanitised them, because a recording can be edited by hand and the page must not be the place
where that goes wrong. What reaches the page is decided here, by an allowlist and not a blocklist:

* An event is kept only if its type has a fixed label template in `LABELS`. The label is written from the template, never copied from the file.
* A fact is kept only if its key is in `FACTS`, `METRICS`, `PERMISSIONS` or `CALL_FACTS` **and** its value has the shape that key allows: an
  integer, a boolean, a gate id, a hash cut to its first 12 hex characters, or **one word of a closed list** (`CHOICES`). No key accepts
  open text, so a token that happens to look like a short label has nowhere to go. A miss is dropped, never repaired.
* The only names copied from a recording are a tool's name and version, in the shape a package manifest allows, and a ToolForge version
  number. Act ids, task ids, meta-tool names and the few words in `CHOICES` must be ones this file lists.
* A step with no real event says so (`outcome: none`). Nothing is invented to fill it. A gate that was skipped says it was skipped.

The lists copied here from the code (statuses, error types, decoy event types, meta-tool names, acts) are compared with their owners by
`tests/test_tour_data.py`, so this script can stay standard library only.

When no directory is named and `demo/recordings/main` does not exist, or with `--illustrative`, the output is the sample story from
`site/src/tour_illustrative.json`. Its events carry no number, only labels and booleans, and the file says `source: illustrative`, so the page
shows its badge instead of a date. A directory named on the command line that does not exist is an error.

Only the standard library is needed. When the repository's `demo.recording` and its secret scanner can be imported, the recording is checked
with `demo.recording.validate` first and a recording that fails is refused; when they cannot, the script says so on stderr and the allowlist
above is the only guard.

Proven by `tests/test_tour_data.py` (the shape, determinism, hostile text and a fake secret, the illustrative fallback).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DEFAULT_RECORDING = ROOT / "demo" / "recordings" / "main"
DEFAULT_OUT = SITE / "data" / "tour.json"
ILLUSTRATIVE = SITE / "src" / "tour_illustrative.json"
SCHEMA = 1
RECORDING_FILES = ("recording.json", "events.json", "transcript.json")
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_EVENTS_PER_STEP = 40

#: The twelve steps, in order. The captions file must have exactly these keys (`tests/test_tour_captions.py`).
STEP_IDS = (
    "meet",
    "find",
    "forge",
    "read_build",
    "test",
    "second_opinion",
    "trap",
    "sign_off",
    "use",
    "reuse",
    "caught",
    "your_turn",
)
#: The gates each step is about; the stage highlights them and a gate step's outcome is read from their events.
STEP_GATES: dict[str, tuple[str, ...]] = {
    "read_build": ("G1", "G2"),
    "test": ("G3", "G4"),
    "second_opinion": ("G5",),
    "trap": ("G6",),
    "sign_off": ("G7",),
    "caught": ("G1", "G6"),
}

# --- Closed lists. Each one that mirrors a list in the code is compared with it by a test. -------------------------------------

#: The lifecycle statuses (`toolforge.registry.lifecycle.STATUSES`).
STATUSES = (
    "submitted",
    "verifying",
    "rejected",
    "verified_basic",
    "verified",
    "pending_approval",
    "active",
    "deprecated",
    "quarantined",
)
#: How a call can fail (`toolforge.models.results.CallError`, plus the runner's `PolicyViolation` and `Aborted`).
ERROR_TYPES = (
    "InvalidArguments",
    "InvalidFiles",
    "ToolError",
    "Crash",
    "NotJson",
    "OutputTooLarge",
    "Timeout",
    "SandboxCrashed",
    "NoResult",
    "PolicyViolation",
    "Aborted",
)
#: MiniLocker's decoy events that mean a tool touched a decoy (`toolforge.sandbox.base.DECOY_VIOLATION_TYPES`).
DECOY_VIOLATIONS = ("canary.read", "canary.exposed", "canary.tripped", "canary.copied")
DECOY_PLANTED = "canary.seeded"
#: The words each `choice` fact may hold. A value outside its list is dropped.
CHOICES: dict[str, tuple[str, ...]] = {
    "from": STATUSES,
    "to": STATUSES,
    "error_type": ERROR_TYPES,
    "submitted_by": ("agent", "operator", "builder", "repair"),
    "real_secret_audit": ("clean", "found", "not_measured"),
    "injection_scan": ("measured", "not measured"),
}
#: The agent's meta-tools (`toolforge.server.tools.META_TOOLS`). A transcript call to any other name is dropped.
META_TOOLS = (
    "forge_find",
    "forge_describe",
    "forge_run",
    "forge_submit",
    "forge_status",
    "forge_list",
    "forge_recipe",
    "forge_runtime_info",
    "forge_build",
    "forge_repair",
    "forge_report_problem",
    "forge_session_close",
    "forge_session_rewind",
    "forge_session_list",
    "forge_chain",
)
#: Which act plays the forge, the reuse and the attack parts (`demo/acts.py`).
FORGE_ACT, REUSE_ACT, ATTACK_ACT = "forge", "reuse", "attack"
ACT_IDS = ("forge", "reuse", "attack", "software")
#: The task of the forge act, in plain words, by task id. An id that is not here is not copied to the page.
TASK_TEXT = {
    "sqlite_join": "Answer a question that needs a join over a SQLite database",
    "fixed_width": "Read a made-up binary format described in a spec",
    "tlv_convert": "Convert a made-up record format described in a spec",
}
UNKNOWN_TASK = "A task from the recorded demo"
NO_TOOL = "not recorded"
ILLUSTRATIVE_STORY = {
    "task": "Read a made-up binary format described in a spec",
    "tool": "binary_reader@1.0.0",
}
ILLUSTRATIVE_BADGE = "Illustrated example, not a recording"
ILLUSTRATIVE_CAVEAT = "Every event in this tour is an invented sample. Nothing here was measured or recorded."
STAND_IN_CAVEAT = (
    "The sandbox in this recording is a stand-in that gives no isolation. It shows the flow of a run, "
    "not the security of the real sandbox."
)
SCRIPTED_CAVEAT = "The agent's replies were scripted. No language model chose what to do."

# --- Shapes -------------------------------------------------------------------------------------------------------------------

GATE = re.compile(r"G[1-7]")
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX12 = re.compile(r"[0-9a-f]{12}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
VERSION = re.compile(r"\d{1,4}(?:\.\d{1,4}){1,3}(?:(?:a|b|rc|\.post|\.dev)\d{1,4})?")
#: A tool name as a manifest allows it (`toolforge.models.package.NAME_PATTERN`), with an optional MAJOR.MINOR.PATCH.
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{2,40}(?:@\d{1,6}\.\d{1,6}\.\d{1,6})?")

#: payload key -> kind. The only keys that can reach the page from an event's own payload.
FACTS: dict[str, str] = {
    "gate": "gate",
    "duration_ms": "int",
    "hints": "int",
    "from": "choice",
    "to": "choice",
    "ok": "bool",
    "error_type": "choice",
    "result_bytes": "int",
    "args_bytes": "int",
    "file_bytes": "int",
    "decoy_events": "int",
    "egress_attempts": "int",
    "truncated": "bool",
    "sandbox_timed_out": "bool",
    "cleanup_ok": "bool",
    "quarantined": "bool",
    "real_secret_audit": "choice",
    "submitted_by": "choice",
    "resumed": "bool",
    "code_sha": "hash",
    "args_sha256": "hash",
    "network": "int",
    "secrets": "int",
}
#: the same for a gate event's `metrics`.
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
    "injection_scan": "choice",
}
#: the same for the `permissions` summary of a submission.
PERMISSIONS: dict[str, str] = {"browser": "bool", "inputs": "bool", "network_hosts": "int", "secrets": "int"}
#: the same for one agent call in the transcript.
CALL_FACTS: dict[str, str] = {
    "step": "int",
    "ok": "bool",
    "duration_ms": "int",
    "argument_chars": "int",
    "result_chars": "int",
    "arguments_sha256": "hash",
}
#: Every fact key the page may be given, with its kind.
ALL_FACTS: dict[str, str] = {**FACTS, **METRICS, **PERMISSIONS, **CALL_FACTS}

#: The label of an event, written from its type. `{gate}` is filled from the validated gate id and `{tool}` from the validated meta-tool
#: name; nothing else from a recording is ever read for a label.
LABELS: dict[str, str] = {
    "forge.submitted": "Tool submitted for inspection",
    "forge.verifying": "Inspection started",
    "forge.gate.started": "{gate} started",
    "forge.gate.passed": "{gate} passed",
    "forge.gate.failed": "{gate} failed",
    "forge.gate.skipped": "{gate} skipped",
    "forge.verified": "Inspection finished",
    "forge.rejected": "Tool rejected",
    "forge.approval.requested": "Approval requested from the operator",
    "forge.approval.granted": "Operator approved the request",
    "forge.approval.denied": "Approval request denied",
    "forge.promoted": "Operator moved the tool onto the shelf",
    "forge.quarantined": "Tool quarantined",
    "forge.call.started": "Call started",
    "forge.call.finished": "Call finished",
    "canary.seeded": "Decoy credentials planted",
    "canary.read": "A decoy credential was read",
    "canary.exposed": "A decoy credential appeared in output",
    "canary.tripped": "A decoy credential was reached for",
    "canary.copied": "A decoy credential was copied",
    "agent.call": "Agent called {tool}",
}
EVENT_KEYS = ("t", "type", "label", "facts", "source", "act", "tool")
#: What a long step may lose, first to last. A rejection, a quarantine, a failed gate and a decoy event are never dropped.
ROUTINE = (
    "forge.gate.started",
    "forge.call.started",
    "forge.verifying",
    "forge.gate.passed",
    "forge.submitted",
)


class TourDataError(Exception):
    """A problem with the recording or the authored files that the person can fix; `main` prints it and exits 1."""


def label_for(kind: str, facts: dict[str, Any], tool: str) -> str:
    """The fixed label of an event type, with the gate id or meta-tool name filled in when the template has a slot for it."""
    return LABELS[kind].format(gate=facts.get("gate") or "A gate", tool=tool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**10


def shaped(key: str, value: Any) -> Any:
    """The value of fact `key` cut to its shape, or `None` when it does not fit. A miss is dropped, never repaired."""
    kind = ALL_FACTS.get(key)
    if kind == "int":
        return value if _is_int(value) else None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "choice":
        return value if isinstance(value, str) and value in CHOICES[key] else None
    if kind == "gate":
        return value if isinstance(value, str) and GATE.fullmatch(value) else None
    if kind == "hash":
        if isinstance(value, str) and (HEX64.fullmatch(value) or HEX12.fullmatch(value)):
            return value[:12]
    return None


def _pick(source: Any, table: dict[str, str]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    kept = {}
    for key in table:
        if key in source and (value := shaped(key, source[key])) is not None:
            kept[key] = value
    return kept


def clean_facts(payload: Any) -> dict[str, Any]:
    """The facts of one ledger event: its own keys, its gate `metrics` and its `permissions`, flattened. Every other key is dropped."""
    if not isinstance(payload, dict):
        return {}
    facts = _pick(payload, FACTS)
    for key, value in _pick(payload.get("metrics"), METRICS).items():
        facts.setdefault(key, value)
    for key, value in _pick(payload.get("permissions"), PERMISSIONS).items():
        facts.setdefault(key, value)
    return dict(sorted(facts.items()))


def _tool(value: Any) -> str | None:
    return value if isinstance(value, str) and TOOL_NAME.fullmatch(value) else None


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 10**7:
        return None
    return round(float(value), 3)


# --- Reading a recording ------------------------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise TourDataError(f"{path} is larger than a recording or a story can be")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise TourDataError(f"{path} cannot be read as JSON: {type(exc).__name__}") from exc


def _validate_with_repository(directory: Path) -> bool:
    """Refuse a recording that `demo.recording.validate` finds fault with. False when that check cannot be imported here."""
    added = [str(ROOT), str(ROOT / "src")]
    sys.path[:0] = added
    try:
        from demo import recording  # noqa: PLC0415

        problems = list(recording.validate(directory))
    except ImportError:
        return False
    except Exception as exc:  # a file of a shape the check did not expect is a bad recording, not a crash
        raise TourDataError(
            f"the recording is not used: it could not be checked ({type(exc).__name__})"
        ) from exc
    finally:
        for entry in added:
            sys.path.remove(entry)
    if problems:
        raise TourDataError(f"the recording is not used: {problems[0]}")
    return True


def ledger_events(raw: Any) -> list[dict[str, Any]]:
    """The recording's ledger events, cut to what the tour may show. An event whose type has no label template is dropped."""
    events = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind, act, tool = item.get("type"), item.get("act"), _tool(item.get("tool"))
        if not isinstance(kind, str) or kind not in LABELS or kind == "agent.call":
            continue
        if act not in ACT_IDS or tool is None:
            continue
        facts = clean_facts(item.get("facts"))
        events.append(
            {
                "act": act,
                "facts": facts,
                "label": label_for(kind, facts, tool),
                "source": "ledger",
                "t": _seconds(item.get("t")),
                "tool": tool,
                "type": kind,
            }
        )
    return events


def agent_calls(raw: Any) -> list[dict[str, Any]]:
    """What the transcript says each act's agent did: the meta-tool called and the sizes and hashes of that turn, never its words."""
    calls = []
    acts = raw.get("acts") if isinstance(raw, dict) else None
    for act in acts if isinstance(acts, list) else []:
        if not isinstance(act, dict) or act.get("act") not in ACT_IDS:
            continue
        task = act.get("task")
        task = task if isinstance(task, str) and task in TASK_TEXT else None
        for call in act.get("calls") if isinstance(act.get("calls"), list) else []:
            if not isinstance(call, dict) or call.get("tool") not in META_TOOLS:
                continue
            calls.append(
                {
                    "act": act["act"],
                    "facts": dict(sorted(_pick(call, CALL_FACTS).items())),
                    "label": label_for("agent.call", {}, call["tool"]),
                    "source": "agent",
                    "t": None,
                    "task": task,
                    "tool": call["tool"],
                    "type": "agent.call",
                }
            )
    return calls


# --- Choosing the events of each step -----------------------------------------------------------------------------------------


def _gate_events(events: list[dict[str, Any]], act: str, gates: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["act"] == act and e["type"].startswith("forge.gate.") and e["facts"].get("gate") in gates
    ]


def _of_type(events: list[dict[str, Any]], act: str, *types: str) -> list[dict[str, Any]]:
    return [e for e in events if e["act"] == act and e["type"] in types]


def _calls(calls: list[dict[str, Any]], act: str, *tools: str) -> list[dict[str, Any]]:
    return [c for c in calls if c["act"] == act and c["tool"] in tools]


def _first_per_tool(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first event of each (tool, type): a decoy event can repeat once per input, and one line says it."""
    seen: set[tuple[str, str]] = set()
    kept = []
    for event in events:
        key = (event["tool"], event["type"])
        if key not in seen:
            seen.add(key)
            kept.append(event)
    return kept


def _decoys_during(events: list[dict[str, Any]], gate_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The decoy events a tool produced while the given gate ran on it (between that gate's first and last event)."""
    found: list[dict[str, Any]] = []
    for tool in dict.fromkeys(e["tool"] for e in gate_events):
        mine = [e for e in gate_events if e["tool"] == tool]
        times = [e["t"] for e in mine if e["t"] is not None]
        if not times:
            continue
        found += [
            e
            for e in events
            if e["act"] == mine[0]["act"]
            and e["tool"] == tool
            and e["type"].startswith("canary.")
            and e["t"] is not None
            and min(times) <= e["t"] <= max(times)
        ]
    return _first_per_tool(found)


def _caught_tools(events: list[dict[str, Any]]) -> list[str]:
    """Tools of the attack act that were stopped: rejected, or quarantined, in the order that happened."""
    seen: list[str] = []
    for event in events:
        stopped = event["type"] in ("forge.rejected", "forge.quarantined")
        if event["act"] == ATTACK_ACT and stopped and event["tool"] not in seen:
            seen.append(event["tool"])
    return seen


def _chronological(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ledger events by time. The sort is stable, so events written in the same millisecond keep the recording's order."""
    return sorted(events, key=lambda e: (e["t"] is None, e["t"] or 0.0))


def _trim(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """At most `limit` events, losing routine ones first so that the event that explains a step stays."""
    kept = list(events)
    for kind in ROUTINE:
        for index in range(len(kept) - 1, -1, -1):
            if len(kept) <= limit:
                return kept
            if kept[index]["type"] == kind:
                del kept[index]
    return kept[-limit:]


def select(step: str, events: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every recorded event that belongs to `step`: the agent's turns first (they carry no time), then the ledger events in time order."""
    forge, reuse, attack = FORGE_ACT, REUSE_ACT, ATTACK_ACT
    chosen: list[dict[str, Any]] = []
    if step == "find":
        chosen = _calls(calls, forge, "forge_find")[:1]
    elif step == "forge":
        chosen = _calls(calls, forge, "forge_submit")[:1] + _of_type(events, forge, "forge.submitted")[:1]
    elif step in ("read_build", "test", "second_opinion"):
        chosen = _gate_events(events, forge, STEP_GATES[step])
    elif step == "trap":
        gate = _gate_events(events, forge, STEP_GATES[step])
        chosen = gate + _decoys_during(events, gate)
    elif step == "sign_off":
        chosen = _gate_events(events, forge, STEP_GATES[step]) + _of_type(
            events,
            forge,
            "forge.verified",
            "forge.approval.requested",
            "forge.approval.granted",
            "forge.approval.denied",
            "forge.promoted",
        )
    elif step == "use":
        chosen = _calls(calls, forge, "forge_run")[:1] + _of_type(
            events, forge, "forge.call.started", "forge.call.finished"
        )
    elif step == "reuse":
        chosen = _calls(calls, reuse, "forge_find", "forge_run") + _of_type(
            events, reuse, "forge.submitted", "forge.call.started", "forge.call.finished"
        )
    elif step == "caught":
        tools = _caught_tools(events)
        mine = [e for e in events if e["act"] == attack and e["tool"] in tools]
        decoys = _first_per_tool([e for e in mine if e["type"].startswith("canary.")])
        chosen = [e for e in mine if e["type"].startswith("forge.")] + decoys
    # "meet" and "your_turn" are about the story and the quick start, not about an event
    spoken = [e for e in chosen if e["source"] == "agent"]
    written = _chronological([e for e in chosen if e["source"] == "ledger"])
    return spoken + written


def outcome_of(step: str, chosen: list[dict[str, Any]]) -> str:
    """`none` (nothing recorded), `skipped` (every gate result is a skip), `failed` (a gate failed) or `recorded`."""
    if not chosen:
        return "none"
    results = [
        e["type"]
        for e in chosen
        if e["type"] in ("forge.gate.passed", "forge.gate.failed", "forge.gate.skipped")
    ]
    if step in STEP_GATES and results and step != "caught":
        if all(kind == "forge.gate.skipped" for kind in results):
            return "skipped"
        if "forge.gate.failed" in results:
            return "failed"
    return "recorded"


def _skipped_gates(chosen: list[dict[str, Any]]) -> list[str]:
    return [e["facts"]["gate"] for e in chosen if e["type"] == "forge.gate.skipped" and "gate" in e["facts"]]


def note_for(
    step: str,
    outcome: str,
    chosen: list[dict[str, Any]],
    isolated: bool | None,
    events: list[dict[str, Any]],
) -> str:
    """Plain sentences about what the recording holds for the step, from fixed templates. They state no number and guess no reason."""
    if outcome == "none":
        return "No event recorded for this step."
    stand_in = isolated is False
    if outcome == "skipped":
        text = "This gate was skipped in the recording, so it checked nothing here."
        if step == "trap" and stand_in:
            text += " The sandbox in this recording is a stand-in that gives no isolation."
        return text
    if outcome == "failed":
        return "A gate failed in this recording: the tool was stopped at that gate."
    kinds = {e["type"] for e in chosen}
    if step in STEP_GATES and step not in ("sign_off", "caught") and (skipped := _skipped_gates(chosen)):
        return f"Skipped in this recording: {', '.join(skipped)}. The other events listed below are real."
    if step == "find":
        return "The recording keeps the size of the search reply, not its words."
    if step == "sign_off":
        if kinds & {"forge.approval.requested", "forge.approval.granted", "forge.approval.denied"}:
            return "The recording holds the approval request and what became of it."
        if "forge.promoted" not in kinds:
            return "The recording holds the inspection result. It holds no decision by a person."
        submitted = next(
            (e["facts"] for e in events if e["act"] == FORGE_ACT and e["type"] == "forge.submitted"), {}
        )
        if submitted.get("network_hosts") == 0 and submitted.get("secrets") == 0:
            return (
                "The recording holds the operator's promotion. This tool asked for no network "
                "and no secrets, so no approval request was needed."
            )
        return "The recording holds the operator's promotion."
    if step == "use":
        if "forge.call.finished" not in kinds:
            return "The recording holds the agent's request, and no finished call."
        text = "These events show one call: the request, its start and how it ended."
        return text + (" The sandbox here is a stand-in that gives no isolation." if stand_in else "")
    if step == "reuse":
        rebuilt = any(
            e["act"] == REUSE_ACT
            and (e["type"] in ("forge.submitted", "forge.verifying") or e["type"].startswith("forge.gate."))
            for e in events
        )
        if rebuilt:
            return "This act holds a submission or a gate run, so something was built again."
        return "This act holds no submission and no gate run: nothing was rebuilt."
    if step == "caught":
        touched = bool(kinds & set(DECOY_VIOLATIONS)) or any(
            e["facts"].get("decoy_events", 0) > 0 for e in chosen
        )
        if touched:
            return "In this recording a tool was stopped after it touched a decoy credential."
        if "forge.quarantined" in kinds:
            return (
                "In this recording a tool was quarantined. No decoy event was recorded, so the decoy catch "
                "is not shown here."
            )
        failed = [
            e["facts"]["gate"] for e in chosen if e["type"] == "forge.gate.failed" and "gate" in e["facts"]
        ]
        if not failed:
            where = ""
        elif failed[0] in ("G1", "G2"):
            where = f" at {failed[0]}, before its own tests or calls ran"
        else:
            where = f" at {failed[0]}"
        return (
            f"In this recording a tool was rejected{where}. No decoy event was recorded, "
            "so the decoy catch is not shown here."
        )
    return "The events below come from the recording."


# --- Assembling the file ------------------------------------------------------------------------------------------------------


def _story(calls: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, str]:
    task_id = next((c["task"] for c in calls if c["act"] == FORGE_ACT and c.get("task")), None)
    tool = next((e["tool"] for e in events if e["act"] == FORGE_ACT and e["type"] == "forge.submitted"), None)
    return {"task": TASK_TEXT.get(task_id or "", UNKNOWN_TASK), "tool": tool or NO_TOOL}


def _public(event: dict[str, Any]) -> dict[str, Any]:
    """The event as the page gets it: only these keys."""
    return {key: event[key] for key in EVENT_KEYS if key in event}


def recording_badge(recorded_at: str, version: str) -> str:
    return f"Recorded from a real run on {recorded_at}, ToolForge {version}"


def from_recording(directory: Path) -> dict[str, Any]:
    names = sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []
    if names != sorted(RECORDING_FILES):
        raise TourDataError(
            f"a recording is exactly {', '.join(RECORDING_FILES)}; {directory} has {', '.join(names) or 'nothing'}"
        )
    if not _validate_with_repository(directory):
        print(
            "build_tour_data: warning: the repository's recording check cannot be imported, so only the allowlist "
            "guards this recording (pip install -e . to run the check)",
            file=sys.stderr,
        )
    meta, raw_events, transcript = (_read_json(directory / name) for name in RECORDING_FILES)
    if not isinstance(meta, dict) or not isinstance(raw_events, dict):
        raise TourDataError("recording.json and events.json must each be an object")
    version, recorded = meta.get("toolforge_version"), meta.get("recorded_at")
    if not (isinstance(version, str) and VERSION.fullmatch(version)) or not (
        isinstance(recorded, str) and DATE.match(recorded)
    ):
        raise TourDataError(
            "recording.json needs a date (recorded_at) and a version number (toolforge_version)"
        )
    recorded = recorded[:10]
    events = ledger_events(raw_events.get("events"))
    calls = agent_calls(transcript)
    isolated = meta.get("isolated") if isinstance(meta.get("isolated"), bool) else None
    agent = meta.get("agent") if meta.get("agent") in ("scripted", "model") else None
    steps = []
    for step in STEP_IDS:
        every = select(step, events, calls)
        shown = _trim(every, MAX_EVENTS_PER_STEP)
        outcome = outcome_of(step, every)
        note = note_for(step, outcome, every, isolated, events)
        if len(shown) < len(every):
            note += " Some routine events are left out to keep the list short."
        steps.append(
            {
                "id": step,
                "gates": list(STEP_GATES.get(step, ())),
                "outcome": outcome,
                "note": note,
                "events": [_public(e) for e in shown],
            }
        )
    caveats = []
    if isolated is not True:
        caveats.append(
            STAND_IN_CAVEAT
            if isolated is False
            else "This recording does not say whether its sandbox isolated anything."
        )
    if agent == "scripted":
        caveats.append(SCRIPTED_CAVEAT)
    return {
        "schema": SCHEMA,
        "source": "recording",
        "recorded_at": recorded,
        "toolforge_version": version,
        "badge": recording_badge(recorded, version),
        "caveats": caveats,
        "recording": {
            "agent": agent,
            "isolated": isolated,
            "mode": meta.get("mode") if meta.get("mode") in ("replay", "live") else None,
            "sandbox": meta.get("sandbox") if meta.get("sandbox") in ("docker", "stand-in") else None,
        },
        "story": _story(calls, events),
        "steps": steps,
    }


def illustrative(source: Path = ILLUSTRATIVE) -> dict[str, Any]:
    """The invented sample story. Its events are cut by the same shapes, and it has no number to show."""
    raw = _read_json(source)
    entries = raw.get("steps") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise TourDataError(f"{source} has no list of steps")
    by_id = {s.get("id"): s for s in entries if isinstance(s, dict)}
    steps = []
    for step in STEP_IDS:
        events = []
        for item in by_id.get(step, {}).get("events", []):
            if not isinstance(item, dict) or item.get("type") not in LABELS:
                continue
            facts = {k: v for k, v in clean_facts(item.get("facts")).items() if isinstance(v, (str, bool))}
            tool = ILLUSTRATIVE_STORY["tool"]
            if item["type"] == "agent.call":
                tool = item.get("tool") if item.get("tool") in META_TOOLS else "forge_find"
            events.append(
                {
                    "t": None,
                    "type": item["type"],
                    "label": label_for(item["type"], facts, tool),
                    "facts": facts,
                    "source": "illustrative",
                    "act": "sample",
                    "tool": tool,
                }
            )
        steps.append(
            {
                "id": step,
                "gates": list(STEP_GATES.get(step, ())),
                "outcome": "illustrative" if events else "none",
                "note": "Invented sample events that show the shape of real ones."
                if events
                else "No event recorded for this step.",
                "events": events,
            }
        )
    return {
        "schema": SCHEMA,
        "source": "illustrative",
        "recorded_at": None,
        "toolforge_version": None,
        "badge": ILLUSTRATIVE_BADGE,
        "caveats": [ILLUSTRATIVE_CAVEAT],
        "recording": None,
        "story": dict(ILLUSTRATIVE_STORY),
        "steps": steps,
    }


def build(recording: Path | None, *, force_illustrative: bool = False) -> dict[str, Any]:
    """The tour data. With no recording directory (or `force_illustrative`) it is the sample story, and says so."""
    if force_illustrative or recording is None or not recording.exists():
        return illustrative()
    return from_recording(recording)


# --- Checking a document ------------------------------------------------------------------------------------------------------

OUTCOMES = ("none", "skipped", "failed", "recorded", "illustrative")


def validate(data: Any) -> list[str]:
    """Everything wrong with a tour data document, as short sentences (an empty list means it is clean)."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"]
    problems: list[str] = []
    source, recorded, version = data.get("source"), data.get("recorded_at"), data.get("toolforge_version")
    if source == "recording":
        if not (isinstance(recorded, str) and DATE.fullmatch(recorded)) or not (
            isinstance(version, str) and VERSION.fullmatch(version)
        ):
            problems.append("a recording needs a date and a version number")
        elif data.get("badge") != recording_badge(recorded, version):
            problems.append("the badge must name the date and version of the recording")
    elif source == "illustrative":
        if recorded is not None or version is not None:
            problems.append("the illustrated example has no date and no version")
        if data.get("badge") != ILLUSTRATIVE_BADGE:
            problems.append("the badge must say that this is an illustrated example")
    else:
        problems.append("source must be recording or illustrative")
    caveats = data.get("caveats")
    if not isinstance(caveats, list) or not all(isinstance(c, str) for c in caveats):
        problems.append("caveats must be a list of sentences")
    story = data.get("story")
    if not isinstance(story, dict) or set(story) != {"task", "tool"}:
        problems.append("story needs a task and a tool")
    steps = data.get("steps")
    if not isinstance(steps, list) or [s.get("id") for s in steps if isinstance(s, dict)] != list(STEP_IDS):
        return [*problems, f"steps must be exactly {', '.join(STEP_IDS)}, in that order"]
    for step in steps:
        name = step["id"]
        missing = [key for key in ("gates", "outcome", "note", "events") if key not in step]
        if missing:
            problems.append(f"step {name} lacks {', '.join(missing)}")
            continue
        if step["gates"] != list(STEP_GATES.get(name, ())):
            problems.append(f"step {name} names the wrong gates")
        if step["outcome"] not in OUTCOMES:
            problems.append(f"step {name} has an unknown outcome")
        if not isinstance(step["note"], str) or not isinstance(step["events"], list):
            problems.append(f"step {name} needs a note and a list of events")
            continue
        if (step["outcome"] == "none") != (not step["events"]):
            problems.append(f"step {name}: the outcome is none exactly when there is no event")
        if len(step["events"]) > MAX_EVENTS_PER_STEP:
            problems.append(f"step {name} has too many events")
        for index, event in enumerate(step["events"]):
            problems += [f"step {name} event {index} {p}" for p in _event_problems(event)]
    return problems


def _event_problems(event: Any) -> list[str]:
    if not isinstance(event, dict) or set(event) != set(EVENT_KEYS):
        return ["does not have exactly the allowed keys"]
    kind, facts, tool = event["type"], event["facts"], event["tool"]
    if kind not in LABELS or not isinstance(facts, dict):
        return ["has an unknown type or no facts"]
    found = []
    allowed_tool = tool in META_TOOLS if kind == "agent.call" else _tool(tool) is not None
    if not allowed_tool:
        found.append("names a tool that is not allowed")
    if event["label"] != label_for(kind, facts, str(tool)):
        found.append("has a label that is not from the template")
    if event["source"] not in ("ledger", "agent", "illustrative"):
        found.append("has an unknown source")
    if event["act"] not in (*ACT_IDS, "sample"):
        found.append("names an unknown act")
    if event["t"] is not None and _seconds(event["t"]) is None:
        found.append("has a time that is not a number of seconds")
    for key, value in facts.items():
        if shaped(key, value) != value or shaped(key, value) is None:
            found.append(f"has a fact that is not allowed: {key if key in ALL_FACTS else 'unknown key'}")
    return found


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def write(data: dict[str, Any], out: Path) -> None:
    problems = validate(data)
    if problems:
        raise TourDataError("the tour data is not valid: " + problems[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write site/data/tour.json from a recorded demo run.")
    parser.add_argument(
        "recording",
        nargs="?",
        type=Path,
        help="a directory made by demo/run_demo.py --record (default: demo/recordings/main)",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="where to write (default: site/data/tour.json)"
    )
    parser.add_argument(
        "--illustrative", action="store_true", help="write the invented sample story, not a recording"
    )
    args = parser.parse_args(argv)
    try:
        if args.recording is not None and not args.illustrative and not args.recording.is_dir():
            raise TourDataError(f"there is no recording directory {args.recording}")
        data = build(args.recording or DEFAULT_RECORDING, force_illustrative=args.illustrative)
        write(data, args.out)
    except TourDataError as exc:
        print(f"build_tour_data: {exc}", file=sys.stderr)
        return 1
    events = sum(len(step["events"]) for step in data["steps"])
    quiet = [step["id"] for step in data["steps"] if step["outcome"] == "none"]
    print(f"build_tour_data: wrote {args.out} (source: {data['source']}, {events} events)")
    if quiet:
        print(f"build_tour_data: no event recorded for: {', '.join(quiet)}")
    if data["source"] == "illustrative" and not args.illustrative:
        print(
            "build_tour_data: warning: no recording found, so this is the illustrated example",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
