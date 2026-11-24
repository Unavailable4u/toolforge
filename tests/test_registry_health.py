"""Failure captures, problem reports and repair attempts as the registry stores them (P12)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolforge.errors import RegistryError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord, health

Make = Callable[..., ToolPackage]
Activate = Callable[[VersionRecord], VersionRecord]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(tmp_path: Path, clock: Clock) -> Iterator[Registry]:
    with Registry(tmp_path / "home", clock=clock) as reg:
        yield reg


@pytest.fixture
def tool(registry: Registry, make_package: Make, activate: Activate) -> VersionRecord:
    return activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))


SHAPE = {"args": {"t": "object", "keys": 1}, "files": {"count": 0, "bytes": 0}}


def fail(registry: Registry, tool: VersionRecord, args: dict | None = None, error: str = "Crash") -> int:
    text = json.dumps(args, sort_keys=True) if args is not None else None
    return registry.record_failure(
        tool.id, error, SHAPE, input_json=text, input_sha256=f"sha-{text}" if text else ""
    )


# --- Failure captures ------------------------------------------------------------------------------------------------------


def test_a_failure_keeps_its_shape_and_only_the_input_it_was_given(
    registry: Registry, tool: VersionRecord
) -> None:
    fail(registry, tool)
    fail(registry, tool, {"text": "boom"}, "Timeout")
    newest, oldest = registry.failures("word_count")
    assert (
        newest.error_class == "Timeout"
        and newest.args == {"text": "boom"}
        and newest.shape == SHAPE
        and newest.version == "1.0.0"
    )
    assert oldest.args is None
    assert [f.args for f in registry.regression_inputs("word_count")] == [{"text": "boom"}]


def test_the_same_input_is_kept_once(registry: Registry, tool: VersionRecord) -> None:
    fail(registry, tool, {"text": "same"})
    fail(registry, tool, {"text": "same"})
    assert len(registry.failures("word_count")) == 2 and len(registry.regression_inputs("word_count")) == 1


def test_only_the_newest_inputs_are_kept_and_older_ones_are_blanked_not_deleted(
    registry: Registry, tool: VersionRecord
) -> None:
    for n in range(health.RAW_INPUTS_PER_TOOL + 5):
        fail(registry, tool, {"text": f"case {n}"})
    kept = registry.regression_inputs("word_count", limit=100)
    assert len(kept) == health.RAW_INPUTS_PER_TOOL and kept[0].args == {
        "text": f"case {health.RAW_INPUTS_PER_TOOL + 4}"
    }
    assert (
        len(registry.failures("word_count", limit=100)) == health.RAW_INPUTS_PER_TOOL + 5
    )  # the rows, and their shapes, stay


def test_inputs_are_per_tool(
    registry: Registry, tool: VersionRecord, make_package: Make, activate: Activate
) -> None:
    other = activate(registry.add_version(make_package("pdf_text", "1.0.0"), created_by="agent"))
    fail(registry, tool, {"text": "mine"})
    fail(registry, other, {"text": "theirs"})
    assert [f.args for f in registry.regression_inputs("word_count")] == [{"text": "mine"}]


def test_a_failure_for_an_unknown_version_or_a_non_json_shape_is_refused(
    registry: Registry, tool: VersionRecord
) -> None:
    with pytest.raises(RegistryError):
        registry.record_failure(999, "Crash", SHAPE)
    with pytest.raises(RegistryError):
        registry.record_failure(tool.id, "Crash", {"x": float("nan")})


def test_a_window_of_outcomes_starts_again_when_a_version_changes_status(
    registry: Registry, tool: VersionRecord, clock: Clock
) -> None:
    for _ in range(3):
        clock.advance(seconds=1)
        registry.record_call(tool.id, False, 5, "Crash")
    assert len(registry.recent_outcomes(tool.id, 20)) == 3
    clock.advance(seconds=1)
    registry.transition(tool.id, "quarantined", actor="operator", reason="test")
    clock.advance(seconds=1)
    registry.transition(tool.id, "active", actor="operator", reason="released")
    assert registry.recent_outcomes(tool.id, 20) == []  # the failures that pulled it do not pull it again
    clock.advance(seconds=1)
    registry.record_call(tool.id, True, 5)
    assert registry.recent_outcomes(tool.id, 20) == [(True, None)]


def test_outcomes_come_newest_first_and_stop_at_the_count(
    registry: Registry, tool: VersionRecord, clock: Clock
) -> None:
    for ok in (True, False, False, True):
        clock.advance(seconds=1)
        registry.record_call(tool.id, ok, 5, None if ok else "Timeout")
    assert registry.recent_outcomes(tool.id, 3) == [(True, None), (False, "Timeout"), (False, "Timeout")]


# --- Problem reports -------------------------------------------------------------------------------------------------------


def test_a_report_is_stored_for_the_operator_and_changes_nothing(
    registry: Registry, tool: VersionRecord
) -> None:
    report_id = registry.add_problem_report(tool.id, "agent", "  the totals are wrong  ")
    (report,) = registry.problem_reports(name="word_count")
    assert (
        report.id == report_id
        and report.description == "the totals are wrong"
        and report.version == "1.0.0"
        and report.reporter == "agent"
    )
    assert registry.count_problem_reports(tool.id) == 1
    refreshed = registry.get_version(tool.id)
    assert refreshed is not None and refreshed.status == "active"


def test_a_report_is_capped_in_length_and_must_say_something(registry: Registry, tool: VersionRecord) -> None:
    registry.add_problem_report(tool.id, "agent", "x" * 5000)
    assert len(registry.problem_reports(version_id=tool.id)[0].description) == health.MAX_REPORT_CHARS
    with pytest.raises(RegistryError, match="needs a description"):
        registry.add_problem_report(tool.id, "agent", "   ")


def test_reporters_are_rate_limited_per_hour_and_the_limit_lifts(
    registry: Registry, tool: VersionRecord, clock: Clock
) -> None:
    for n in range(health.REPORTS_PER_HOUR):
        registry.add_problem_report(tool.id, "agent", f"report {n}")
    with pytest.raises(RegistryError) as raised:
        registry.add_problem_report(tool.id, "agent", "one too many")
    assert raised.value.code == "report_rate_limited"
    registry.add_problem_report(tool.id, "another agent", "a different reporter is not limited by the first")
    clock.advance(hours=1, minutes=1)
    registry.add_problem_report(tool.id, "agent", "an hour later")


def test_a_version_holds_a_bounded_number_of_reports(
    registry: Registry, tool: VersionRecord, clock: Clock
) -> None:
    for n in range(health.MAX_REPORTS_PER_VERSION):
        clock.advance(hours=1)  # keep every report under the per-hour limit
        registry.add_problem_report(tool.id, "agent", f"report {n}")
    clock.advance(hours=1)
    with pytest.raises(RegistryError) as raised:
        registry.add_problem_report(tool.id, "agent", "the twenty-first")
    assert raised.value.code == "report_cap"
    assert registry.count_problem_reports(tool.id) == health.MAX_REPORTS_PER_VERSION


def test_reports_are_listed_newest_first_for_one_version_or_one_tool(
    registry: Registry, tool: VersionRecord
) -> None:
    registry.add_problem_report(tool.id, "agent", "first")
    registry.add_problem_report(tool.id, "agent", "second")
    assert [r.description for r in registry.problem_reports(name="word_count")] == ["second", "first"]
    assert registry.problem_reports(name="nothing") == []


# --- Repairs ---------------------------------------------------------------------------------------------------------------


def test_repair_attempts_are_limited_per_tool_per_hour(
    registry: Registry, tool: VersionRecord, clock: Clock
) -> None:
    for _ in range(3):
        repair_id = registry.begin_repair("word_count", "1.0.0", max_per_hour=3)
        registry.finish_repair(repair_id, "verified", "1.0.1")
    with pytest.raises(RegistryError) as raised:
        registry.begin_repair("word_count", "1.0.0", max_per_hour=3)
    assert raised.value.code == "repair_rate_limited"
    assert registry.begin_repair("other_tool", "1.0.0", max_per_hour=3)  # another tool has its own allowance
    clock.advance(hours=1, minutes=1)
    assert registry.begin_repair("word_count", "1.0.0", max_per_hour=3)


def test_three_failed_repairs_in_a_row_flag_the_version_and_a_success_resets_the_count(
    registry: Registry, tool: VersionRecord
) -> None:
    for _ in range(2):
        registry.finish_repair(registry.begin_repair("word_count", "1.0.0", max_per_hour=99), "failed")
    registry.finish_repair(
        registry.begin_repair("word_count", "1.0.0", max_per_hour=99), "aborted"
    )  # says nothing about the repair
    state = registry.repair_state("word_count", "1.0.0")
    assert (state.failed_in_a_row, state.flagged) == (2, False)
    registry.finish_repair(registry.begin_repair("word_count", "1.0.0", max_per_hour=99), "failed")
    assert registry.repair_state("word_count", "1.0.0").flagged
    with pytest.raises(RegistryError) as raised:
        registry.begin_repair("word_count", "1.0.0", max_per_hour=99)
    assert raised.value.code == "repair_flagged" and "toolforge problems" in raised.value.hint
    assert not registry.repair_state("word_count", "1.0.1").flagged  # the flag belongs to the base version


def test_a_verified_repair_ends_the_streak(registry: Registry, tool: VersionRecord) -> None:
    for outcome in ("failed", "failed", "verified", "failed"):
        registry.finish_repair(registry.begin_repair("word_count", "1.0.0", max_per_hour=99), outcome)
    assert registry.repair_state("word_count", "1.0.0").failed_in_a_row == 1


def test_a_repair_ends_once_and_only_in_a_known_way(registry: Registry, tool: VersionRecord) -> None:
    repair_id = registry.begin_repair("word_count", "1.0.0", max_per_hour=9)
    with pytest.raises(RegistryError):
        registry.finish_repair(repair_id, "started")
    registry.finish_repair(repair_id, "verified", "1.0.1")
    registry.finish_repair(repair_id, "failed")  # already ended: ignored
    (repair,) = registry.repairs("word_count")
    assert (repair.outcome, repair.child_version, repair.base_version) == ("verified", "1.0.1", "1.0.0")
