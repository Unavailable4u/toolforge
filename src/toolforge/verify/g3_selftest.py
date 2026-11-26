"""Gate G3, self-tests: the package's own tests and examples, in a fresh sandbox with no network, and a check that the tests mean something.

Three runs in one sandbox (four when the tool has regression cases), all through the in-sandbox harness (never pytest, never anything the
author supplied):

1. `--tests`: every `tests/test_*.py` (top-level `test_*` functions and `unittest.TestCase` methods), each in its own process.
2. `--batch` over the manifest examples that give an `output` (and are not `live`): the result must equal the expected output as
   JSON, numbers within 1e-9.
2b. Regression cases (P12): argument objects that crashed, timed out or killed the sandbox in an earlier version of this tool, stored by
   `store_failed_inputs`. They go through `--batch` too. Each must end in a result or a handled `ToolError`; a crash, a timeout or an
   oversized or non-JSON result fails the gate, and the hint names the case by its number, never by its content.
3. `--tests --stub`: the same tests with the entrypoint replaced by a function that returns `None`. If every test still passes the
   tests do not constrain behaviour (`assert True`), and the gate fails.

Session tools (P13, `kind: session`) are checked as sequences. Steps 2 and 2b run with the harness in session mode: the examples run in the
order the manifest lists them and share one state, as the calls of one session would. Then a fresh sandbox runs the first example again
and must give the same answer, which shows that the state starts empty and that nothing but the saved state carries over from one call
to the next. Regression cases run on a state reset to empty, since the cases are only argument objects.

Limits of the test runner, documented in `docs/verification.md`: no fixtures, no `parametrize`, no `conftest.py`, no async tests, plain
`assert`; a test file may import `tool` and its own helpers from `tests/`, and nothing else is on the path but the package and its
`.deps`. The strength check proves tests are not vacuous; it does not prove they are good.
"""

from __future__ import annotations

import json
import math
from typing import Any

from toolforge.verify import plan_gates, sandboxrun
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
        if plan_gates.is_plan(ctx):
            # a browser plan is data: it runs against recorded pages, with no sandbox
            return plan_gates.dry_run(ctx)
        if ctx.blob is None:
            return failed(
                "there is no built package to test",
                ["G2 must pass first; this is a ToolForge problem if you see it"],
            )
        manifest = ctx.package.manifest
        limits = sandboxrun.limits_of(ctx)
        session = sandboxrun.is_session(ctx)
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
                bad = self._examples(handle, base, limits, examples, metrics, session)
                if bad is not None:
                    bad.metrics = metrics
                    return bad
            if ctx.regression_cases:
                bad = self._regressions(handle, base, limits, ctx.regression_cases, metrics, session)
                if bad is not None:
                    bad.metrics = metrics
                    return bad
            stub = self._tests(handle, base, stub=True)
            if isinstance(stub, GateResult):
                stub.metrics = metrics
                return stub
        if session and examples:
            bad = self._fresh_session(ctx, base, limits, examples[0], metrics)
            if bad is not None:
                bad.metrics = metrics
                return bad
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
        session: bool = False,
    ) -> GateResult | None:
        items = [{"args": e.input, "files": e.files} for _, e in examples]
        request = {**base, "items": items, "include_results": True, "deadline_s": EXAMPLES_DEADLINE_S}
        if session:
            request["session"] = True
            sandboxrun.reset_state(handle)
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

    # -- a second, fresh session (session tools) ----------------------------------------------------------------------------

    @staticmethod
    def _fresh_session(
        ctx: GateContext,
        base: dict[str, Any],
        limits: dict[str, int],
        first: tuple[int, Any],
        metrics: dict[str, Any],
    ) -> GateResult | None:
        """The first example again, in a new sandbox with no state. It must give the answer it gave at the start of the first session."""
        number, example = first
        request = {
            **base,
            "items": [{"args": example.input, "files": example.files}],
            "include_results": True,
            "deadline_s": EXAMPLES_DEADLINE_S,
            "session": True,
        }
        with sandboxrun.open_sandbox(ctx, "G3", network="none") as fresh:
            sandboxrun.load_package(fresh, ctx.blob or b"")
            run = sandboxrun.run_harness(
                fresh,
                ["--batch"],
                request,
                timeout_s=EXAMPLES_DEADLINE_S + sandboxrun.GRACE_S,
                max_out=limits["output_kb"] * 1024 * 6 + 64 * 1024,
            )
        read = sandboxrun.read_batch_report(run.report, 1) if run.usable else None
        if read is None or not read[0]:
            return failed(
                sandboxrun.describe_failed_run(run, "running the first example in a fresh session"),
                ["a new session must be able to start from an empty state"],
            )
        outcome = read[0][0]
        metrics["fresh_session_checked"] = True
        if not outcome.ok or not json_equal(example.output, outcome.result):
            what = outcome.error_type if not outcome.ok else _short_json(outcome.result)
            return failed(
                f"example {number} gave a different answer in a fresh session than at the start of the first one",
                [
                    f"fresh session: {what}; manifest: {_short_json(example.output)}",
                    "a session may keep only what is in `state`; a tool whose first answer depends on anything else is not repeatable",
                ],
            )
        return None

    # -- regression cases -----------------------------------------------------------------------------------------------

    @staticmethod
    def _regressions(
        handle: Any,
        base: dict[str, Any],
        limits: dict[str, int],
        cases: tuple[dict[str, Any], ...],
        metrics: dict[str, Any],
        session: bool = False,
    ) -> GateResult | None:
        items = [{"args": case, "files": {}} for case in cases]
        request = {**base, "items": items, "include_results": False, "deadline_s": EXAMPLES_DEADLINE_S}
        if session:
            request["session"] = True
            sandboxrun.reset_state(handle)
        run = sandboxrun.run_harness(
            handle,
            ["--batch"],
            request,
            timeout_s=EXAMPLES_DEADLINE_S + sandboxrun.GRACE_S,
            max_out=len(items) * 4096 + 64 * 1024,
        )
        read = sandboxrun.read_batch_report(run.report, len(items)) if run.usable else None
        if read is None:
            return failed(
                sandboxrun.describe_failed_run(run, "running the regression cases"),
                ["the tool must return from every call; do not call os._exit or hang"],
            )
        outcomes, not_run = read
        bad = [(n, o) for n, o in enumerate(outcomes, start=1) if not o.ok and o.error_type != "ToolError"]
        metrics["regression_cases"] = len(items)
        metrics["regression_cases_failed"] = len(bad)
        if bad or not_run:
            hints = [
                f"regression case {n} (an input that broke an earlier version) still fails with {o.error_type}: {clip(o.message, 120)}"
                for n, o in bad[:6]
            ]
            if not_run:
                hints.append(f"{not_run} regression cases did not run before the time limit")
            hints.append(
                "fix the cause, or refuse such input with ToolError and a clear message; do not make the case disappear"
            )
            return failed(
                f"{len(bad) + not_run} of {len(items)} regression cases still break the tool", hints
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
