"""Gate G3, self-tests: the package's own tests and examples, in a fresh sandbox with no network, and a check that the tests mean something.

Three runs in one sandbox, all through the in-sandbox harness (never pytest, never anything the author supplied):

1. `--tests`: every `tests/test_*.py` (top-level `test_*` functions and `unittest.TestCase` methods), each in its own process.
2. `--batch` over the manifest examples that give an `output` (and are not `live`): the result must equal the expected output as
   JSON, numbers within 1e-9.
3. `--tests --stub`: the same tests with the entrypoint replaced by a function that returns `None`. If every test still passes the
   tests do not constrain behaviour (`assert True`), and the gate fails.

Limits of the test runner, documented in `docs/verification.md`: no fixtures, no `parametrize`, no `conftest.py`, no async tests, plain
`assert`; a test file may import `tool` and its own helpers from `tests/`, and nothing else is on the path but the package and its
`.deps`. The strength check proves tests are not vacuous; it does not prove they are good.
"""

from __future__ import annotations

import json
import math
from typing import Any

from toolforge.verify import sandboxrun
from toolforge.verify.pipeline import GateContext, GateResult, clip, failed, passed
from toolforge.verify.sandboxrun import TestReport

TEST_TIMEOUT_S = 10
TESTS_DEADLINE_S = 120
EXAMPLES_DEADLINE_S = 60
TOLERANCE = 1e-9
_STUB_BROKEN = ("cannot stub the entrypoint", "has no function")


def json_equal(expected: Any, actual: Any) -> bool:
    """JSON equality where numbers are compared with a tolerance of 1e-9. `True` is not `1`; key order does not matter."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(expected, actual, rel_tol=TOLERANCE, abs_tol=TOLERANCE)
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(json_equal(expected[k], actual[k]) for k in expected)
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            json_equal(a, b) for a, b in zip(expected, actual, strict=True)
        )
    return type(expected) is type(actual) and expected == actual


def _short_json(value: Any, limit: int = 90) -> str:
    try:
        return clip(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False), limit)
    except (TypeError, ValueError, RecursionError):
        return "(not JSON)"


class SelfTestGate:
    id = "G3"
    budget_s = 360
    OPTIONS: tuple[str, ...] = ()

    def run(self, ctx: GateContext) -> GateResult:
        if ctx.blob is None:
            return failed(
                "there is no built package to test",
                ["G2 must pass first; this is a ToolForge problem if you see it"],
            )
        manifest = ctx.package.manifest
        limits = sandboxrun.limits_of(ctx)
        base = {"entrypoint": manifest.entrypoint, "limits": limits}
        examples = [
            (n, e) for n, e in enumerate(manifest.examples, start=1) if e.output is not None and not e.live
        ]
        skipped_live = sum(1 for e in manifest.examples if e.live)
        metrics: dict[str, Any] = {"examples_checked": 0, "examples_live_skipped": skipped_live}
        with sandboxrun.open_sandbox(ctx, "G3", network="none") as handle:
            sandboxrun.load_package(handle, ctx.blob)
            real = self._tests(handle, base, stub=False)
            if isinstance(real, GateResult):
                return real
            metrics.update(self._metrics(real))
            problem = self._test_problem(real)
            if problem is not None:
                problem.metrics = metrics
                return problem
            if examples:
                bad = self._examples(handle, base, limits, examples, metrics)
                if bad is not None:
                    bad.metrics = metrics
                    return bad
            stub = self._tests(handle, base, stub=True)
            if isinstance(stub, GateResult):
                stub.metrics = metrics
                return stub
        return self._strength(real, stub, metrics)

    # -- the three runs -------------------------------------------------------------------------------------------------

    @staticmethod
    def _tests(handle: Any, base: dict[str, Any], *, stub: bool) -> TestReport | GateResult:
        request = {**base, "test_timeout_s": TEST_TIMEOUT_S, "deadline_s": TESTS_DEADLINE_S}
        run = sandboxrun.run_harness(
            handle,
            ["--tests", *(["--stub"] if stub else [])],
            request,
            timeout_s=TESTS_DEADLINE_S + sandboxrun.GRACE_S,
            max_out=128 * 1024,
        )
        what = "the strength check (tests with the tool replaced by a stub)" if stub else "the test run"
        report = sandboxrun.read_test_report(run.report) if run.usable else None
        if report is None:
            return failed(
                sandboxrun.describe_failed_run(run, what),
                [
                    "a test must not call os._exit, hang, or print the harness's markers; run the tests locally with plain Python first"
                ],
            )
        return report

    @staticmethod
    def _metrics(report: TestReport) -> dict[str, Any]:
        return {
            "tests_ran": report.ran,
            "tests_passed": report.passed,
            "tests_failed": report.failed_count,
            "tests_errors": report.error_count,
            "tests_skipped": report.skipped,
        }

    @staticmethod
    def _test_problem(report: TestReport) -> GateResult | None:
        if report.ran == 0 and report.error_count == 0:
            return failed(
                "no tests were found",
                [
                    "add tests in tests/test_*.py: top-level functions named test_..., or unittest.TestCase methods",
                    "fixtures, parametrize and conftest.py are not supported; call the tool's function directly and use plain assert",
                ],
            )
        if not report.clean:
            first = report.first_problem
            hints = []
            if first is not None:
                hints.append(f"first problem: {first.id}: {first.error}")
            hints += [f"{item.id}: {item.error}" for item in (*report.failed, *report.errors)[1:4]]
            if report.not_run:
                hints.append(
                    f"{report.not_run} tests did not run before the time limit; make the tests faster"
                )
            if any("fixtures" in item.error or "async" in item.error for item in report.errors):
                hints.append(
                    "only plain test functions and unittest.TestCase methods are supported (no fixtures, parametrize or async)"
                )
            total = report.failed_count + report.error_count
            return failed(
                f"{total} of {report.ran} tests did not pass"
                + (f" and {report.not_run} did not run" if report.not_run else ""),
                hints,
            )
        if report.passed == 0:
            return failed(
                "every test was skipped, so nothing was checked",
                ["remove the skips or make the tests run"],
            )
        return None

    def _examples(
        self,
        handle: Any,
        base: dict[str, Any],
        limits: dict[str, int],
        examples: list[tuple[int, Any]],
        metrics: dict[str, Any],
    ) -> GateResult | None:
        items = [{"args": e.input, "files": e.files} for _, e in examples]
        request = {**base, "items": items, "include_results": True, "deadline_s": EXAMPLES_DEADLINE_S}
        run = sandboxrun.run_harness(
            handle,
            ["--batch"],
            request,
            timeout_s=EXAMPLES_DEADLINE_S + sandboxrun.GRACE_S,
            max_out=len(items) * (limits["output_kb"] * 1024 * 6) + 64 * 1024,
        )
        read = sandboxrun.read_batch_report(run.report, len(items)) if run.usable else None
        if read is None:
            return failed(
                sandboxrun.describe_failed_run(run, "running the manifest examples"),
                ["make sure each example runs on its own with the tool's entrypoint"],
            )
        outcomes, not_run = read
        wrong: list[str] = []
        for (number, example), outcome in zip(examples, outcomes, strict=False):
            if not outcome.ok:
                wrong.append(f"example {number} failed with {outcome.error_type}: {outcome.message}")
            elif not json_equal(example.output, outcome.result):
                wrong.append(
                    f"example {number} returned {_short_json(outcome.result)} but the manifest says {_short_json(example.output)}"
                )
        metrics["examples_checked"] = len(outcomes)
        metrics["examples_failed"] = len(wrong)
        if not_run:
            wrong.append(f"{not_run} examples did not run before the time limit")
        if wrong:
            return failed(
                f"{len(wrong)} of {len(items)} examples did not give the expected result",
                [*wrong[:6], "fix the tool, or fix the example's output so the manifest tells the truth"],
            )
        return None

    # -- the strength check ---------------------------------------------------------------------------------------------

    @staticmethod
    def _strength(real: TestReport, stub: TestReport, metrics: dict[str, Any]) -> GateResult:
        broken = [item for item in (*stub.errors, *stub.failed) if any(m in item.error for m in _STUB_BROKEN)]
        if broken:
            return failed(
                "the strength check could not replace the entrypoint",
                [
                    f"{broken[0].error}",
                    "the entrypoint must be a function in a .py file that the tests can import",
                ],
                metrics=metrics,
            )
        noticed = stub.failed_count + stub.error_count
        metrics["tests_failing_with_stub"] = noticed
        if noticed == 0:
            return failed(
                f"your tests do not constrain behaviour: all {stub.passed} still pass when the tool returns nothing",
                [
                    "assert on the returned values, for example assert run({...}) == {...}",
                    "a test that only calls the tool or asserts True checks nothing",
                ],
                metrics=metrics,
            )
        return passed(
            f"{real.passed} tests passed, the examples matched, and {noticed} of {stub.ran} tests notice when the tool returns nothing",
            metrics=metrics,
        )


__all__ = ["SelfTestGate", "json_equal"]
