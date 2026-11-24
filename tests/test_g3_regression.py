"""G3 and regression cases (P12): inputs that broke an earlier version are run against the new one, through the same harness."""

from __future__ import annotations

from typing import Any

from sandbox_scripts import batch_report, item, mode_of, respond
from test_g3_selftest import EXAMPLE_OUT, EXAMPLES, good_answers
from verify_helpers import valid_package

from toolforge.config import Settings
from toolforge.sandbox import FakeRunner
from toolforge.verify.g3_selftest import SelfTestGate
from toolforge.verify.pipeline import GateContext

CASES = ({"text": "\tcase one"}, {"text": "case two"})


def run_with(cases: tuple[dict[str, Any], ...], regression: Any):
    answers = good_answers(
        batch=lambda request: (
            regression(request)
            if not request["include_results"]
            else batch_report([item(0, EXAMPLE_OUT), item(1, {"text": "a", "words": 1})])
        )
    )
    package = valid_package(examples=EXAMPLES)
    runner = FakeRunner(lambda argv, put: respond(argv, put, answers))
    ctx = GateContext(
        package=package, settings=Settings(), runner=runner, label="my_tool@0.1.0", deadline=1e12,
        blob=package.to_tar(), regression_cases=cases,
    )  # fmt: skip
    return SelfTestGate().run(ctx), runner


def test_no_cases_means_no_extra_run() -> None:
    result, runner = run_with((), lambda request: batch_report([]))
    assert result.status == "passed" and [mode_of(c.argv) for c in runner.handles[0].calls] == [
        "tests",
        "batch",
        "stub",
    ]
    assert "regression_cases" not in result.metrics


def test_the_cases_are_run_after_the_examples_with_the_same_sandbox_and_no_network() -> None:
    seen: dict[str, Any] = {}

    def regression(request):
        seen.update(request)
        return batch_report([item(0, {"text": "x", "words": 1}), item(1, error=("ToolError", "refused"))])

    result, runner = run_with(CASES, regression)
    assert result.status == "passed", result.hints
    assert [mode_of(c.argv) for c in runner.handles[0].calls] == ["tests", "batch", "batch", "stub"]
    assert len(runner.opened) == 1 and runner.opened[0].network == "none"
    assert [i["args"] for i in seen["items"]] == list(CASES) and seen["include_results"] is False
    assert result.metrics["regression_cases"] == 2 and result.metrics["regression_cases_failed"] == 0


def test_a_handled_tool_error_is_an_acceptable_answer_to_an_old_crash() -> None:
    result, _ = run_with(
        CASES,
        lambda request: batch_report(
            [item(0, error=("ToolError", "bad input")), item(1, error=("ToolError", "bad input"))]
        ),
    )
    assert result.status == "passed"


def test_a_case_that_still_crashes_fails_the_gate_and_is_named_by_number_not_content() -> None:
    result, _ = run_with(
        CASES,
        lambda request: batch_report(
            [item(0, {"text": "x", "words": 1}), item(1, error=("Crash", "KeyError 'text'"))]
        ),
    )
    assert result.status == "failed" and result.summary == "1 of 2 regression cases still break the tool"
    joined = " ".join(result.hints)
    assert (
        "regression case 2" in joined
        and "Crash" in joined
        and "case two" not in joined
        and "\\tcase one" not in joined
    )
    assert result.metrics["regression_cases_failed"] == 1


def test_a_case_that_hangs_or_is_not_run_fails_the_gate() -> None:
    result, _ = run_with(CASES, lambda request: batch_report([item(0, {"text": "x", "words": 1})], not_run=1))
    assert result.status == "failed" and "did not run" in " ".join(result.hints)
