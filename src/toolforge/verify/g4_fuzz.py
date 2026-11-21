"""Gate G4, schema fuzz: generated inputs that the tool's own `input_schema` allows, run in one sandbox, one process per input.

The inputs come from `verify.schemagen` (boundary values of every property, plus the manifest examples changed one property at a
time), seeded by the package's `code_sha`, so the same package always gets the same inputs. They run through the harness `--batch`
mode with the package's own timeout and output limit.

A run passes when every input ends in a result or a `ToolError` (the tool refusing on purpose, which is handled). It fails on a crash,
a timeout, an oversized or non-JSON result, or when the time budget ends before every input has run. The first few failing inputs
are named in the hints, described by what is unusual about them, never by dumping them.

The number of inputs is a constructor parameter (`FuzzGate(fuzz_inputs=30)`), not a setting. Not measured here: whether the answers
are right. This gate finds crashes and hangs, nothing more.
"""

from __future__ import annotations

from typing import Any

from toolforge import schemacheck
from toolforge.verify import sandboxrun, schemagen
from toolforge.verify.pipeline import GateContext, GateResult, clip, failed, passed, skipped

DEFAULT_INPUTS = 30
MAX_INPUTS = 200
BUDGET_S = 120
HINTED_FAILURES = 3
ALLOWED_ERRORS = ("ToolError",)


def p95(values: list[int]) -> int:
    """The 95th percentile (nearest rank) of a list of millisecond values; 0 for an empty list."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


class FuzzGate:
    id = "G4"
    budget_s = BUDGET_S + 90
    OPTIONS: tuple[str, ...] = ("fuzz_inputs",)

    def __init__(self, fuzz_inputs: int = DEFAULT_INPUTS) -> None:
        if (
            isinstance(fuzz_inputs, bool)
            or not isinstance(fuzz_inputs, int)
            or not 1 <= fuzz_inputs <= MAX_INPUTS
        ):
            raise ValueError(f"fuzz_inputs must be a whole number from 1 to {MAX_INPUTS}")
        self.count = fuzz_inputs

    def run(self, ctx: GateContext) -> GateResult:
        if ctx.blob is None:
            return failed(
                "there is no built package to test",
                ["G2 must pass first; this is a ToolForge problem if you see it"],
            )
        manifest = ctx.package.manifest
        try:
            inputs = schemagen.generate(
                manifest.input_schema, manifest.examples, seed=ctx.package.code_sha, count=self.count
            )
        except schemacheck.SchemaCheckError as exc:
            return failed(
                "the input schema could not be checked in time while generating inputs",
                [f"simplify the input schema ({clip(str(exc), 100)})"],
            )
        if not inputs:
            return skipped(
                "no valid inputs could be generated from the input schema, so nothing was fuzzed",
                [
                    "a schema that forbids every value, or one made only of patterns, gives the fuzzer nothing to try"
                ],
                metrics={"inputs_run": 0},
            )
        limits = sandboxrun.limits_of(ctx)
        items = [{"args": item.args, "files": item.files} for item in inputs]
        request = {
            "entrypoint": manifest.entrypoint,
            "limits": limits,
            "items": items,
            "include_results": False,
            "deadline_s": BUDGET_S,
        }
        with sandboxrun.open_sandbox(ctx, "G4", network="none") as handle:
            sandboxrun.load_package(handle, ctx.blob)
            run = sandboxrun.run_harness(
                handle,
                ["--batch"],
                request,
                timeout_s=BUDGET_S + sandboxrun.GRACE_S,
                max_out=len(items) * 4096,
            )
        read = sandboxrun.read_batch_report(run.report, len(items)) if run.usable else None
        if read is None:
            return failed(
                sandboxrun.describe_failed_run(run, "the fuzz run"),
                ["the tool must return from every call; do not call os._exit or hang"],
                metrics={"inputs_planned": len(items)},
            )
        outcomes, not_run = read
        bad = [(inputs[o.index], o) for o in outcomes if not o.ok and o.error_type not in ALLOWED_ERRORS]
        handled = sum(1 for o in outcomes if not o.ok and o.error_type in ALLOWED_ERRORS)
        durations = [o.duration_ms for o in outcomes]
        metrics: dict[str, Any] = {
            "inputs_planned": len(items),
            "inputs_run": len(outcomes),
            "handled_errors": handled,
            "failures": len(bad),
            "p95_ms": p95(durations),
            "total_ms": sum(durations),
        }
        detail: dict[str, Any] = {
            "failing_inputs": [
                {"label": clip(item.label, 120), "error_type": o.error_type, "message": o.message}
                for item, o in bad[:10]
            ]
        }
        if bad or not_run:
            hints = [
                f"input with {clip(item.label, 90)}: {o.error_type}: {o.message}"
                for item, o in bad[:HINTED_FAILURES]
            ]
            if not_run:
                hints.append(
                    f"only {len(outcomes)} of {len(items)} inputs ran before the {BUDGET_S} second budget; make the tool faster"
                )
            if any(o.error_type == "Crash" for _, o in bad):
                hints.append(
                    "refuse bad input with ToolError and a clear message instead of letting an exception escape"
                )
            return failed(
                f"{len(bad)} of {len(outcomes)} generated inputs broke the tool"
                + (f", and {not_run} did not run in time" if not_run else ""),
                hints,
                metrics=metrics,
                detail=detail,
            )
        if outcomes and handled == len(outcomes):
            detail["warnings"] = [
                "G4: every generated input was refused with ToolError, so the tool's main path was not exercised "
                "(for example the inputs name a file the fuzzer cannot supply); the examples in G3 are what covers it"
            ]
        return passed(
            f"all {len(outcomes)} generated inputs ended in a result or a handled refusal ({handled} refusals)",
            metrics=metrics,
            detail=detail,
        )


__all__ = ["DEFAULT_INPUTS", "FuzzGate", "p95"]
