"""A recording holds allowlisted facts only: no environment values, no source, no secret-shaped strings, no free text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo import recording

ROOT = Path(__file__).resolve().parent.parent
COMMITTED = ROOT / "demo" / "recordings" / "main"


def test_the_committed_recording_validates_and_is_clean() -> None:
    import os

    assert recording.validate(COMMITTED, os.environ) == []


def test_the_committed_recording_says_what_made_it() -> None:
    meta = json.loads((COMMITTED / "recording.json").read_text(encoding="utf-8"))
    assert meta["mode"] in ("replay", "live") and meta["seed"] == 7
    assert meta["sandbox"] in ("docker", "stand-in")
    assert meta["isolated"] is (meta["sandbox"] == "docker")
    assert meta["ledger_files"] == meta["ledger_files_verified"], "every ledger chain should have verified"
    if meta["mode"] == "replay":
        assert meta["agent"] == "scripted"
    assert all(a["checker"] == "accepted" for a in meta["acts"])


def test_the_committed_recording_has_the_gates_of_each_act() -> None:
    events = json.loads((COMMITTED / "events.json").read_text(encoding="utf-8"))["events"]
    by_act: dict[str, set[str]] = {}
    for event in events:
        by_act.setdefault(event["act"], set()).add(event["type"])
    assert "forge.gate.passed" in by_act["forge"] and "forge.rejected" in by_act["attack"]
    assert not any(t.startswith("forge.gate") for t in by_act["reuse"]), (
        "reuse must not build or verify anything"
    )
    assert {"forge.session.opened", "forge.session.rewound", "forge.session.closed"} <= by_act["software"]
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)


def event(kind: str, ts: float, payload: dict[str, object]) -> dict[str, object]:
    return {"type": kind, "ts": ts, "payload": payload}


def test_sanitising_keeps_facts_with_the_right_shape_and_drops_the_rest() -> None:
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    source = "def run(args):\n    return open('/etc/passwd').read()"
    raw = event(
        "forge.gate.failed",
        100.0,
        {
            "gate": "G1",
            "duration_ms": 12,
            "summary": f"found {secret} in the code",
            "source": source,
            "error": "boom",
            "hints": ["a hint"],
            "metrics": {
                "failures": 1,
                "files": 2,
                "injection_scan": "measured",
                "leak": secret,
                "egress": "not applicable (no network)",
                "model": None,
            },
            "permissions": {
                "browser": False,
                "network_hosts": ["a.example.com", "b.example.com"],
                "secrets": 0,
                "evil": 1,
            },
            "code_sha": "ab" * 32,
            "tool": "ignored@1.0.0",
        },
    )
    events, dropped = recording.sanitise([("sqlite_query@1.0.0", "verify", [raw])], {"forge": (0.0, 200.0)})
    assert dropped == 0 and len(events) == 1
    facts = events[0]["facts"]
    assert facts == {
        "gate": "G1",
        "duration_ms": 12,
        "metrics": {
            "failures": 1,
            "files": 2,
            "injection_scan": "measured",
            "egress": "not applicable (no network)",
        },
        "permissions": {"browser": False, "secrets": 0},
        "code_sha": "abababababab",
    }
    text = json.dumps(events)
    assert secret not in text and "passwd" not in text and "boom" not in text and "ignored" not in text


def test_events_of_types_that_are_not_kept_are_dropped_and_counted() -> None:
    files = [
        (
            "t@1.0.0",
            "box",
            [
                event("agent.said", 1.0, {"text": "hi"}),
                event("sandbox.created", 2.0, {"image": "x", "env": {"K": "v"}}),
                event("forge.call.started", 3.0, {}),
            ],
        )
    ]
    events, dropped = recording.sanitise(files, {})
    assert dropped == 1 and [e["type"] for e in events] == ["sandbox.created", "forge.call.started"]
    assert events[0]["facts"] == {}, "native MiniLocker events keep their type only"


def test_a_value_of_the_wrong_shape_is_dropped_not_repaired() -> None:
    facts = recording.facts_of(
        {
            "gate": "G9",
            "duration_ms": "12",
            "ok": 1,
            "error_type": "has space",
            "step": True,
            "network": 3,
            "secrets": ["a", "b"],
        }
    )
    assert facts == {"network": 3, "secrets": 2}


def test_an_event_belongs_to_the_last_act_that_had_started() -> None:
    files = [
        (
            "t@1.0.0",
            "verify",
            [
                event("forge.submitted", 5.0, {}),
                event("forge.submitted", 15.0, {}),
                event("forge.submitted", 1.0, {}),
            ],
        )
    ]
    events, _ = recording.sanitise(files, {"one": (4.0, 10.0), "two": (10.0, 20.0)})
    assert [(e["t"], e["act"]) for e in events] == [(0.0, ""), (4.0, "one"), (14.0, "two")]


def make(tmp_path: Path, events: list[dict[str, object]] | None = None, **meta: object) -> Path:
    full = {
        "recorded_at": "2026-01-01",
        "toolforge_version": "0",
        "seed": 7,
        "mode": "replay",
        "sandbox": "stand-in",
        "isolated": False,
        **meta,
    }
    recording.write_recording(tmp_path, full, events or [], {"schema": 1, "acts": []})
    return tmp_path


def test_a_clean_recording_validates(tmp_path: Path) -> None:
    ok = {
        "t": 0.0,
        "act": "forge",
        "ledger": "verify",
        "tool": "a@1.0.0",
        "type": "forge.submitted",
        "facts": {"submitted_by": "agent"},
    }
    assert recording.validate(make(tmp_path, [ok])) == []


@pytest.mark.parametrize(
    ("event", "needle"),
    [
        ({"t": 0, "type": "forge.submitted", "facts": {"source": "x"}}, "not allowed"),
        ({"t": 0, "type": "forge.submitted", "facts": {"gate": "nope"}}, "shape"),
        ({"t": 0, "type": "agent.said", "facts": {}}, "not kept"),
        ({"t": 0, "type": "forge.submitted", "facts": {}, "text": "hello"}, "keys that are not allowed"),
        ({"t": 0, "type": "forge.submitted", "facts": {"metrics": {"mystery": 1}}}, "not allowed"),
        ({"t": 0, "type": "forge.submitted", "facts": {"code_sha": "abc"}}, "12 hex"),
    ],
)
def test_validation_catches_a_hand_edited_event(
    tmp_path: Path, event: dict[str, object], needle: str
) -> None:
    problems = recording.validate(make(tmp_path, [event]))
    assert any(needle in p for p in problems), problems


def test_validation_finds_a_secret_shaped_string_and_an_environment_value(tmp_path: Path) -> None:
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    made = make(tmp_path, [], note=f"key {secret}")
    assert any("secret-shaped" in p for p in recording.validate(made))
    clean = make(tmp_path / "b", [], note="user is jane-doe-1234")
    assert any("environment value" in p for p in recording.validate(clean, {"USER": "jane-doe-1234"}))


def test_a_recording_is_exactly_three_files(tmp_path: Path) -> None:
    made = make(tmp_path)
    (made / "extra.txt").write_text("x", encoding="utf-8")
    assert any("exactly" in p for p in recording.validate(made))
    with pytest.raises(FileExistsError):
        recording.write_recording(made, {}, [], {})
    assert recording.validate(tmp_path / "missing")[0].startswith("a recording is exactly")


def test_long_or_multiline_strings_never_pass(tmp_path: Path) -> None:
    bad = {"t": 0, "type": "forge.submitted", "facts": {"submitted_by": "a" * 80}}
    assert recording.validate(make(tmp_path, [bad]))
