"""Gates G1 to G4 on a session tool (P13), against the REAL harness through a stand-in sandbox that is not isolated (trusted test code only)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from local_sandbox import LocalRunner
from sessions_helpers import counter_package
from verify_helpers import valid_package

from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.registry import Registry, lifecycle
from toolforge.report import blast_radius
from toolforge.verify.pipeline import Pipeline

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")


@pytest.fixture
def runner():
    local = LocalRunner()
    yield local
    local.cleanup()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


CLEAN_SCHEMA = {
    "type": "object",
    "properties": {"by": {"type": "integer", "minimum": 0, "maximum": 5}},
    "additionalProperties": False,
}


def clean_counter(**changes):
    """The shared counter without its test-only switches (boom, tamper, ...), which G4 would rightly set and trip over."""
    return counter_package(input_schema=CLEAN_SCHEMA, **changes)


def verify(package, runner, settings):
    return Pipeline(settings, runner, None, fuzz_inputs=20).run(package, "test")


def gate(report, name: str):
    return next(g for g in report.gates if g.id == name)


def test_a_counter_passes_g1_to_g4(runner, settings) -> None:
    report = verify(clean_counter(), runner, settings)
    assert {g.id: g.status for g in report.gates if g.id in ("G1", "G2", "G3", "G4")} == {
        "G1": "passed",
        "G2": "passed",
        "G3": "passed",
        "G4": "passed",
    }, report.text()


def test_g3_runs_the_examples_in_order_in_one_session_so_state_carries_from_one_to_the_next(
    runner, settings
) -> None:
    # the second example's answer (3) is only right if the first one's state (1) was kept
    result = gate(verify(clean_counter(), runner, settings), "G3")
    assert result.status == "passed" and result.metrics["examples_checked"] == 2


def test_g3_checks_the_first_example_again_in_a_fresh_session(runner, settings) -> None:
    result = gate(verify(clean_counter(), runner, settings), "G3")
    assert result.metrics["fresh_session_checked"] is True


def test_g3_rejects_an_example_that_does_not_follow_from_the_state_before_it(runner, settings) -> None:
    package = clean_counter(
        examples=[{"input": {"by": 1}, "output": {"n": 1}}, {"input": {"by": 2}, "output": {"n": 2}}]
    )
    result = gate(verify(package, runner, settings), "G3")
    assert result.status == "failed" and any("example 2" in h for h in result.hints)


# A tool that keeps something outside `state`: a file next to the package. The tests (run in the same sandbox) leave it behind, so the
# first example answers 1 there, as its manifest says, but 2 in any new session.
LEAKY = """import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(args, state):
    marker = os.path.join(HERE, "seen.txt")
    again = os.path.exists(marker)
    open(marker, "w").write("x")
    state["n"] = state.get("n", 0) + 1
    return {"n": state["n"] + (0 if again else 1)}
"""


def test_g3_rejects_a_tool_whose_first_answer_depends_on_something_other_than_state(runner, settings) -> None:
    package = valid_package(
        "leaky",
        "0.1.0",
        tool=LEAKY,
        tests=(
            "from tool import run\n\n\n"
            "def test_counts():\n    state = {}\n    assert run({}, state)['n'] in (1, 2)\n    assert state == {'n': 1}\n\n\n"
            "def test_again():\n    state = {'n': 4}\n    assert run({}, state)['n'] in (5, 6)\n    assert state == {'n': 5}\n\n\n"
            "def test_twice():\n    state = {}\n    run({}, state)\n    assert run({}, state)['n'] in (2, 3)\n"
        ),
        kind="session",
        examples=[{"input": {}, "output": {"n": 1}}],
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object"},
    )
    result = gate(verify(package, runner, settings), "G3")
    assert result.status == "failed", result.summary
    assert "fresh session" in result.summary and result.metrics.get("fresh_session_checked") is True


def test_g4_feeds_a_session_tool_in_one_session_without_crashing_it(runner, settings) -> None:
    result = gate(verify(clean_counter(), runner, settings), "G4")
    assert result.status == "passed"


def test_a_submitted_counter_is_registered_and_ends_verified(runner, settings) -> None:
    with Registry(settings.home) as registry:
        report = Pipeline(settings, runner, registry, ledger_factory=ToolforgeLedger, fuzz_inputs=5).run(
            clean_counter(), "test"
        )
        assert report.registered and report.status == lifecycle.VERIFIED_BASIC, report.text()
        record = registry.find_version("counter", "0.1.0")
        assert record is not None and record.status == lifecycle.VERIFIED_BASIC


def test_the_session_report_is_none_when_there_are_no_session_ledgers(settings) -> None:
    assert blast_radius.session_report(settings, "counter", "0.1.0", "s-00000000") is None
