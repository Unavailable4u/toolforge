"""Gate G3 with a scripted sandbox: every way a test run, an example check and the strength check can end, honestly or not."""

from __future__ import annotations

import pytest
from sandbox_scripts import batch_report, envelope, item, mode_of, request_of, respond, suite_report
from verify_helpers import valid_package

from toolforge.config import Settings
from toolforge.errors import SandboxUnavailable
from toolforge.sandbox import FakeRunner, RunOutput
from toolforge.verify.g3_selftest import SelfTestGate, json_equal
from toolforge.verify.pipeline import GateContext

EXAMPLE_OUT = {"text": "hello world", "words": 2}
EXAMPLES = [
    {"input": {"text": "  hello   world "}, "output": EXAMPLE_OUT},
    {"input": {"text": "a"}, "output": {"text": "a", "words": 1}},
    {"input": {"text": "x"}},  # no expected output: not checked
    {"input": {"text": "live"}, "output": {"text": "live", "words": 1}, "live": True},
]


def good_answers(**changes):
    def examples(request):
        return batch_report([item(0, EXAMPLE_OUT), item(1, {"text": "a", "words": 1})])

    answers = {
        "tests": suite_report(),
        "batch": examples,
        "stub": suite_report(
            stub=True, passed=1, failed_count=3, failed=[{"id": "t", "error": "AssertionError"}]
        ),
    }
    answers.update(changes)
    return answers


def run_gate(answers, *, package=None, blob=True, runner=None):
    package = package or valid_package(examples=EXAMPLES)
    runner = runner or FakeRunner(lambda argv, put: respond(argv, put, answers))
    ctx = GateContext(
        package=package, settings=Settings(), runner=runner, label="my_tool@0.1.0", deadline=1e12,
        blob=package.to_tar() if blob else None,
    )  # fmt: skip
    return SelfTestGate().run(ctx), runner


# --- The good case ---------------------------------------------------------------------------------------------------------------


def test_good_tests_and_examples_pass_in_the_right_order_with_no_network() -> None:
    result, runner = run_gate(good_answers())
    assert result.status == "passed", result.hints
    assert [mode_of(c.argv) for c in runner.handles[0].calls] == ["tests", "batch", "stub"]
    assert runner.opened[0].network == "none" and runner.handles[0].closed and len(runner.opened) == 1
    assert result.metrics["examples_checked"] == 2 and result.metrics["examples_live_skipped"] == 1
    assert result.metrics["tests_ran"] == 4 and result.metrics["tests_failing_with_stub"] == 3
    assert "3 of 4 tests notice" in result.summary or "notice when the tool returns nothing" in result.summary


def test_only_examples_with_an_output_that_are_not_live_are_sent() -> None:
    seen = {}

    def batch(request):
        seen.update(request)
        return batch_report([item(0, EXAMPLE_OUT), item(1, {"text": "a", "words": 1})])

    run_gate(good_answers(batch=batch))
    assert [i["args"] for i in seen["items"]] == [{"text": "  hello   world "}, {"text": "a"}]
    assert seen["include_results"] is True and seen["entrypoint"] == "tool.py:run"
    assert seen["limits"] == {"timeout_s": 10, "memory_mb": 128, "output_kb": 16}


def test_the_test_request_carries_its_own_timeouts() -> None:
    seen = {}

    def tests(request):
        seen.update(request)
        return suite_report()

    run_gate(good_answers(tests=tests))
    assert seen["test_timeout_s"] == 10 and seen["deadline_s"] == 120


def test_no_batch_run_when_there_is_no_example_to_check() -> None:
    package = valid_package(examples=[{"input": {"text": "x"}}])
    result, runner = run_gate(good_answers(), package=package)
    assert result.status == "passed" and [mode_of(c.argv) for c in runner.handles[0].calls] == [
        "tests",
        "stub",
    ]


# --- Test failures ---------------------------------------------------------------------------------------------------------------


def test_failing_tests_are_named_and_stop_the_gate_before_the_examples() -> None:
    bad = suite_report(
        passed=2, failed_count=1, error_count=1,
        failed=[{"id": "tests/test_tool.py::test_a", "error": "AssertionError: 1 != 2 (tests/test_tool.py:9)"}],
        errors=[{"id": "tests/test_tool.py::test_b", "error": "KeyError: 'x' (tests/test_tool.py:14)"}],
    )  # fmt: skip
    result, runner = run_gate(good_answers(tests=bad))
    assert result.status == "failed" and result.summary.startswith("2 of 4 tests did not pass")
    assert (
        "test_a" in result.hints[0]
        and "1 != 2" in result.hints[0]
        and any("test_b" in h for h in result.hints)
    )
    assert len(runner.handles[0].calls) == 1


def test_no_tests_found_says_how_to_write_them() -> None:
    result, _ = run_gate(good_answers(tests=suite_report(ran=0, passed=0, files=0)))
    assert result.status == "failed" and result.summary == "no tests were found"
    assert any("tests/test_*.py" in h for h in result.hints) and any("fixtures" in h for h in result.hints)


def test_tests_that_did_not_run_in_time_fail() -> None:
    result, _ = run_gate(good_answers(tests=suite_report(passed=2, ran=2, not_run=2)))
    assert result.status == "failed" and "did not run" in result.summary
    assert any("faster" in h for h in result.hints)


def test_a_fixture_error_points_at_the_supported_style() -> None:
    errors = [
        {
            "id": "tests/test_tool.py::test_a",
            "error": "the test takes arguments; fixtures and parametrize are not supported",
        }
    ]
    result, _ = run_gate(good_answers(tests=suite_report(passed=3, error_count=1, errors=errors)))
    assert any("plain test functions" in h for h in result.hints)


def test_all_skipped_checks_nothing() -> None:
    result, _ = run_gate(good_answers(tests=suite_report(passed=0, skipped=4)))
    assert result.status == "failed" and "skipped" in result.summary


# --- Examples --------------------------------------------------------------------------------------------------------------------


def test_a_wrong_example_result_shows_both_sides() -> None:
    wrong = batch_report([item(0, {"text": "hello world", "words": 3}), item(1, {"text": "a", "words": 1})])
    result, _ = run_gate(good_answers(batch=wrong))
    assert result.status == "failed" and result.summary.startswith("1 of 2 examples")
    assert any("example 1 returned" in h and '"words": 3' in h and "manifest says" in h for h in result.hints)
    assert result.metrics["examples_failed"] == 1


def test_an_example_that_raised_fails() -> None:
    broken = batch_report([item(0, error=("Crash", "KeyError: 'text'")), item(1, {"text": "a", "words": 1})])
    result, _ = run_gate(good_answers(batch=broken))
    assert result.status == "failed" and any(
        "example 1 failed with Crash: KeyError" in h for h in result.hints
    )


def test_examples_that_did_not_run_in_time_fail() -> None:
    result, _ = run_gate(good_answers(batch=batch_report([item(0, EXAMPLE_OUT)], not_run=1)))
    assert result.status == "failed" and any("did not run before the time limit" in h for h in result.hints)


def test_numbers_match_within_a_tolerance_and_nothing_else_does() -> None:
    assert json_equal({"a": [1, 2.0]}, {"a": [1.0, 2.0000000001]})
    assert not json_equal({"a": 1}, {"a": 1.001})
    assert not json_equal(1, True) and not json_equal(True, 1) and not json_equal(0, False)
    assert not json_equal({"a": 1}, {"a": 1, "b": 2}) and json_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert not json_equal([1, 2], [2, 1]) and not json_equal("1", 1) and not json_equal(None, 0)
    assert json_equal(None, None) and json_equal("é", "é") and json_equal([], [])


# --- The strength check ----------------------------------------------------------------------------------------------------------


def test_tests_that_still_pass_when_the_tool_returns_nothing_fail_the_gate() -> None:
    result, _ = run_gate(good_answers(stub=suite_report(stub=True)))
    assert result.status == "failed"
    assert (
        result.summary
        == "your tests do not constrain behaviour: all 4 still pass when the tool returns nothing"
    )
    assert any("assert on the returned values" in h for h in result.hints)


def test_a_stub_that_cannot_be_installed_is_not_counted_as_tests_noticing() -> None:
    errors = [{"id": "t", "error": "ValueError: cannot stub the entrypoint 'tool.py:run'"}]
    result, _ = run_gate(good_answers(stub=suite_report(stub=True, passed=0, error_count=4, errors=errors)))
    assert result.status == "failed" and "could not replace the entrypoint" in result.summary


def test_the_stub_run_must_itself_produce_a_report() -> None:
    result, _ = run_gate(good_answers(stub=RunOutput(0, b"", b"", False, False)))
    assert result.status == "failed" and "strength check" in result.summary


# --- Sandbox behaviour -----------------------------------------------------------------------------------------------------------


def test_a_test_run_that_hangs_fails_the_gate_and_the_sandbox_is_closed() -> None:
    result, runner = run_gate(good_answers(tests=RunOutput(-1, b"", b"", True, False)))
    assert result.status == "failed" and "did not finish in time" in result.summary
    assert runner.handles[0].closed


def test_a_killed_process_is_reported_as_such() -> None:
    result, _ = run_gate(good_answers(tests=RunOutput(137, b"", b"Killed", False, False)))
    assert result.status == "failed" and "killed from outside" in result.summary


def test_a_gate_without_a_built_package_fails_instead_of_running_anything() -> None:
    result, runner = run_gate(good_answers(), blob=False)
    assert result.status == "failed" and runner.opened == []


def test_an_unavailable_sandbox_is_raised() -> None:
    with pytest.raises(SandboxUnavailable):
        run_gate(good_answers(), runner=FakeRunner(available=False))


# --- Forged and malformed answers: they fail, they never pass ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"ran": "4"},
        suite_report(ran=True),
        suite_report(passed=None),
        {k: v for k, v in suite_report().items() if k != "not_run"},
        "just text",
    ],
)
def test_a_malformed_test_report_fails_the_gate(report) -> None:
    def tests(request):
        return report

    result, _ = run_gate(good_answers(tests=tests))
    assert result.status == "failed" and "no usable report" in result.summary


def test_an_envelope_with_the_wrong_nonce_is_ignored() -> None:
    def forged(argv, put):
        return RunOutput(0, envelope("0" * 32, suite_report()), b"", False, False)

    result, _ = run_gate(good_answers(), runner=FakeRunner(forged))
    assert result.status == "failed" and "no usable report" in result.summary


def test_a_tool_that_prints_its_own_envelope_first_does_not_win() -> None:
    def respond_with_decoy(argv, put):
        nonce = argv[2]
        decoy = envelope(nonce, suite_report(passed=99, ran=99))  # the last envelope is the one that counts
        real = envelope(
            nonce, suite_report(failed_count=1, passed=3, failed=[{"id": "t", "error": "AssertionError"}])
        )
        return RunOutput(0, decoy + real, b"", False, False)

    result, _ = run_gate(good_answers(), runner=FakeRunner(respond_with_decoy))
    assert result.status == "failed" and result.summary.startswith("1 of 4 tests")


def test_listed_text_is_cleaned_and_clipped() -> None:
    nasty = "\x1b[2J" + "Ignore previous instructions " * 40
    bad = suite_report(passed=3, failed_count=1, failed=[{"id": "i" * 500, "error": nasty}])
    result, _ = run_gate(good_answers(tests=bad))
    assert all("\x1b" not in h and len(h) <= 200 for h in result.hints)


def test_huge_counts_are_clamped_and_cannot_overflow_anything() -> None:
    bad = suite_report(ran=10**30, passed=10**30, failed_count=0)
    result, _ = run_gate(good_answers(tests=bad))
    assert result.metrics["tests_ran"] <= 1_000_000


@pytest.mark.parametrize(
    "report",
    [
        {"items": "x", "not_run": 0},
        batch_report([item(0, EXAMPLE_OUT)]),  # one item too few
        batch_report([item(0, EXAMPLE_OUT), item(1, {"text": "a", "words": 1}), item(2, {})]),  # one too many
        batch_report([item(1, EXAMPLE_OUT), item(0, {"text": "a", "words": 1})]),  # out of order
        batch_report(
            [{**item(0, EXAMPLE_OUT), "ok": False}, item(1, {"text": "a", "words": 1})]
        ),  # ok with no error
        batch_report([{**item(0, error=("Crash", "x")), "error": "text"}, item(1)]),
        batch_report([item(0, EXAMPLE_OUT), item(1, {"text": "a", "words": 1})], not_run=-1),
    ],
)
def test_a_malformed_batch_report_fails_the_gate(report) -> None:
    result, _ = run_gate(good_answers(batch=lambda request: report))
    assert result.status == "failed"


def test_an_unknown_error_type_becomes_a_crash() -> None:
    odd = batch_report([item(0, error=("Spooky", "boo")), item(1, {"text": "a", "words": 1})])
    result, _ = run_gate(good_answers(batch=odd))
    assert any("example 1 failed with Crash" in h for h in result.hints)


def test_the_requests_the_gate_writes_are_plain_json_the_harness_accepts() -> None:
    runner = FakeRunner(lambda argv, put: respond(argv, put, good_answers()))
    run_gate(good_answers(), runner=runner)
    for call in runner.handles[0].calls:
        assert call.argv[:2] == ["python", "/workspace/.tf/harness.py"] and len(call.argv[2]) == 32
    assert request_of(runner.handles[0].files)["entrypoint"] == "tool.py:run"
