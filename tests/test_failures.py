"""Failure capture and automatic quarantine (P12): what is kept about a failed call, and when a tool that keeps failing is pulled."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_tool_runner import Rig, StandInRegistry, envelope_bytes, error_envelope, out

from toolforge.config import Settings
from toolforge.errors import ToolNotRunnable
from toolforge.services import failures
from toolforge.services import runner as runner_module
from toolforge.services.failures import (
    QUARANTINE_MIN_CALLS,
    QUARANTINE_RATE,
    QUARANTINE_WINDOW,
    Verdict,
    fingerprint,
    quarantine_verdict,
    shape_of,
    stored_input,
)

# --- The shape fingerprint --------------------------------------------------------------------------------------------------


def test_a_fingerprint_describes_types_and_sizes() -> None:
    shape = fingerprint(
        {
            "text": "hello world",
            "top": 3,
            "ratio": -0.5,
            "flags": [True, False],
            "nested": {"a": 1},
            "nothing": None,
        },
        {"data.csv": b"a,b\n1,2\n"},
        declared=["text", "top", "ratio", "flags", "nested", "nothing"],
    )
    args = shape["args"]
    assert args["t"] == "object" and args["keys"] == 6
    assert args["props"]["text"] == {"t": "str", "len": 11}
    assert args["props"]["top"] == {"t": "int", "sign": "pos", "digits": 1}
    assert args["props"]["ratio"] == {"t": "float", "sign": "neg"}
    assert args["props"]["flags"] == {"t": "array", "len": 2, "items": ["bool"]}
    assert args["props"]["nested"] == {"t": "object", "keys": 1, "unnamed_keys": 1}
    assert args["props"]["nothing"] == {"t": "null"}
    assert shape["files"] == {"count": 1, "bytes": 8}


def test_a_fingerprint_contains_no_value_and_no_undeclared_key() -> None:
    secret_text = "hunter2-correct-horse"
    shape = fingerprint(
        {
            "text": secret_text,
            "api_key_sk_live_123": "v",
            "count": 8675309,
            "list": ["zebra-value", "other"],
            "obj": {"inner_key_xyz": "inner-value"},
        },
        {"private_filename.csv": b"zzz"},
        declared=["text", "count", "list", "obj"],
    )
    dumped = json.dumps(shape)
    for leaked in (
        secret_text,
        "api_key_sk_live_123",
        "8675309",
        "zebra-value",
        "inner_key_xyz",
        "inner-value",
        "private_filename",
    ):
        assert leaked not in dumped
    assert shape["args"]["unnamed_keys"] == 1  # the undeclared key is counted, not named
    assert shape["args"]["props"]["count"]["digits"] == 7


def test_only_plain_identifiers_are_named_even_when_declared() -> None:
    shape = fingerprint({"a b": 1, "ok": 2}, None, declared=["a b", "ok"])
    assert list(shape["args"]["props"]) == ["ok"] and shape["args"]["unnamed_keys"] == 1


def test_a_deep_or_huge_value_is_summarised_not_walked() -> None:
    deep: Any = "x"
    for _ in range(10):
        deep = {"k": deep}
    assert shape_of(deep, ["k"]) == {
        "t": "object",
        "keys": 1,
        "props": {"k": {"t": "object", "keys": 1, "unnamed_keys": 1}},
    }
    assert shape_of(list(range(1000))) == {"t": "array", "len": 1000, "items": ["int"]}


# --- What may be stored raw -------------------------------------------------------------------------------------------------


def settings(tmp_path: Path, **changes: Any) -> Settings:
    return Settings(home=tmp_path / "home", **changes)


def test_nothing_raw_is_stored_by_default(tmp_path: Path) -> None:
    assert stored_input(settings(tmp_path), {"text": "hello"}, None) == (None, "")


def test_with_the_setting_on_a_small_plain_input_is_stored_with_its_hash(tmp_path: Path) -> None:
    text, sha = stored_input(settings(tmp_path, store_failed_inputs=True), {"text": "hello"}, None)
    assert json.loads(text) == {"text": "hello"} and len(sha) == 64


@pytest.mark.parametrize(
    ("args", "files"),
    [
        ({"text": "x" * 9000}, None),  # 8 KB or more
        (
            {"text": "hello"},
            {"a.csv": b"1"},
        ),  # a file came with it: the arguments alone would not reproduce the call
        ({"token": "AKIAIOSFODNN7EXAMPLE"}, None),  # secret-shaped
        ({"key": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"}, None),
        ("not an object", None),
    ],
)
def test_an_input_that_is_large_has_files_or_looks_secret_is_never_stored(
    tmp_path: Path, args: Any, files: Any
) -> None:
    assert stored_input(settings(tmp_path, store_failed_inputs=True), args, files) == (None, "")


# --- When a tool is pulled --------------------------------------------------------------------------------------------------


def outcomes(*pairs: str) -> list[tuple[bool, str | None]]:
    """`ok`, or an error class: `outcomes("Crash", "ok")` is newest first, as the registry returns it."""
    return [(True, None) if p == "ok" else (p == "ToolError", p) for p in pairs]


def test_the_limits_are_the_guides() -> None:
    assert (QUARANTINE_WINDOW, QUARANTINE_MIN_CALLS, QUARANTINE_RATE) == (20, 10, 0.40)
    assert failures.QUARANTINE_FAILURES == ("Crash", "Timeout", "SandboxCrashed", "NoResult")


def test_nothing_happens_before_ten_calls() -> None:
    assert quarantine_verdict(outcomes(*["Crash"] * 9)) is None
    assert quarantine_verdict(outcomes(*["Crash"] * 10)) == Verdict(10, 10)


def test_forty_percent_stays_and_more_goes() -> None:
    assert quarantine_verdict(outcomes(*["Crash"] * 4, *["ok"] * 6)) is None  # exactly 40 percent
    verdict = quarantine_verdict(outcomes(*["Crash"] * 5, *["ok"] * 5))
    assert verdict == Verdict(5, 10) and verdict.rate == 0.5


@pytest.mark.parametrize("kind", ["Crash", "Timeout", "SandboxCrashed", "NoResult"])
def test_each_failure_class_counts(kind: str) -> None:
    assert quarantine_verdict(outcomes(*[kind] * 6, *["ok"] * 4)) is not None


def test_handled_tool_errors_are_left_out_altogether() -> None:
    assert quarantine_verdict(outcomes(*["ToolError"] * 20)) is None
    # six crashes among only eight real calls: the twelve refusals neither dilute nor count
    verdict = quarantine_verdict(outcomes(*["Crash"] * 6, *["ToolError"] * 12, "ok", "ok"))
    assert (
        verdict is None
    )  # the window is the last 20 calls, which holds just 8 non-ToolError ones: too few to judge
    assert quarantine_verdict(outcomes(*["Crash"] * 6, *["ToolError"] * 5, *["ok"] * 4, "Crash")) == Verdict(
        7, 11
    )


def test_only_the_last_twenty_calls_are_looked_at() -> None:
    assert quarantine_verdict(outcomes(*["ok"] * 20, *["Crash"] * 30)) is None
    assert quarantine_verdict(outcomes(*["Crash"] * 20, *["ok"] * 30)) == Verdict(20, 20)


def test_output_problems_do_not_pull_a_tool() -> None:
    assert quarantine_verdict(outcomes(*["OutputTooLarge"] * 10, *["NotJson"] * 10)) is None


def test_the_reason_names_the_numbers_and_the_call() -> None:
    reason = Verdict(5, 10).reason("call0001")
    assert "5 of the last 10" in reason and "50%" in reason and "40%" in reason and "call0001" in reason


# --- Through the runner -----------------------------------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path: Path, make_payload: Any, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """The runner tests' rig (same scripted sandbox), rebuilt here so this module owns its fixture."""
    monkeypatch.setattr(runner_module, "ARG_CHECK_ISOLATED", False)
    built = Rig(tmp_path, make_payload)
    built.register()
    built.succeeds()
    return built


class HealthRegistry(StandInRegistry):
    """The stand-in registry plus the two storage calls P12's runner makes."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: list[dict[str, Any]] = []

    def record_failure(self, version_id, error_class, shape, *, input_json=None, input_sha256=""):  # type: ignore[no-untyped-def]
        self.failures.append(
            {
                "version_id": version_id,
                "error_class": error_class,
                "shape": shape,
                "input_json": input_json,
                "sha": input_sha256,
            }
        )
        return len(self.failures)

    def recent_outcomes(self, version_id: int, count: int = 20):  # type: ignore[no-untyped-def]
        rows = [(ok, kind) for vid, ok, _ms, kind, _s in self.calls if vid == version_id]
        return list(reversed(rows))[:count]


@pytest.fixture
def health_rig(rig: Rig) -> Rig:
    from toolforge.services.runner import ToolRunner

    rig.registry = HealthRegistry()
    rig.register()
    rig.runner = ToolRunner(
        rig.fake,
        rig.registry,
        rig.settings,
        lambda path, task: __import__("test_tool_runner").RecordingLedger(rig, path, task),
        clock=lambda: rig.now,
        new_id=lambda: "call0001",
    )
    rig.succeeds()
    return rig


def crash(rig: Rig) -> None:
    rig.answers(lambda nonce: out(error_envelope(nonce, "Crash", "boom")))


def tool_error(rig: Rig) -> None:
    rig.answers(lambda nonce: out(error_envelope(nonce, "ToolError", "no such column")))


def good(rig: Rig) -> None:
    rig.answers(lambda nonce: out(envelope_bytes(nonce)))


def test_a_crash_is_captured_with_its_shape_and_no_value(health_rig: Rig) -> None:
    crash(health_rig)
    result = health_rig.run(args={"text": "hunter2-correct-horse"})
    assert result.ok is False
    (captured,) = health_rig.registry.failures
    assert captured["error_class"] == "Crash" and captured["input_json"] is None and captured["sha"] == ""
    assert captured["shape"]["args"]["props"]["text"] == {"t": "str", "len": 21}
    assert "hunter2" not in json.dumps(captured)


def test_the_raw_input_is_kept_only_when_the_operator_allowed_it(health_rig: Rig) -> None:
    health_rig.settings.store_failed_inputs = True
    health_rig.runner._settings = health_rig.settings  # type: ignore[attr-defined]
    crash(health_rig)
    health_rig.run(args={"text": "kept"})
    assert json.loads(health_rig.registry.failures[0]["input_json"]) == {"text": "kept"}


def test_a_handled_tool_error_is_not_captured(health_rig: Rig) -> None:
    tool_error(health_rig)
    health_rig.run()
    assert health_rig.registry.failures == []


def test_a_tool_is_pulled_on_the_tenth_call_not_before(health_rig: Rig) -> None:
    good(health_rig)
    for _ in range(5):
        health_rig.run()
    crash(health_rig)
    for _ in range(4):  # nine calls: too few to judge
        health_rig.run()
    assert health_rig.registry.transitions == []
    health_rig.run()  # the tenth: five of ten fail
    (moved,) = health_rig.registry.transitions
    assert moved[1:3] == ("quarantined", "forge") and "5 of the last 10 calls failed" in moved[3]
    assert health_rig.registry.records[moved[0]].status == "quarantined"
    health_rig.answers(lambda nonce: out(envelope_bytes(nonce)))
    with pytest.raises(ToolNotRunnable, match="no active version"):
        health_rig.run()  # not callable any more


def test_tool_errors_do_not_count_toward_quarantine(health_rig: Rig) -> None:
    tool_error(health_rig)
    for _ in range(15):
        health_rig.run()
    assert health_rig.registry.transitions == []


def test_a_failure_that_cannot_be_recorded_does_not_break_the_call(health_rig: Rig) -> None:
    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk full")

    health_rig.registry.record_failure = broken  # type: ignore[method-assign]
    crash(health_rig)
    assert health_rig.run().ok is False


def test_a_registry_without_failure_storage_is_fine(rig: Rig) -> None:
    crash(rig)
    assert rig.run().ok is False and rig.registry.transitions == []
