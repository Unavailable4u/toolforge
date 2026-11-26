"""What gates G3, G4 and G6 do for a package of kind `browser_plan` (P14b). A plan is data, so there is no code to test, fuzz or contain.

G1 is the ordinary static gate: `ToolPackage.validate` checks the plan (closed vocabulary, limits, hosts, no templated selector, no
credential field) and the secret scan reads every file. G2 freezes the package as it is. G5 does not review plans and is skipped.

* **G3, the dry run.** Every example that is not `live` and has an `output` is run against the package's recorded pages
  (`fixtures/*.json`, replayed by `FixtureBrowser`) and the fields the plan extracts must equal the example's `output`. A plan with no
  such example, or whose examples all expect nothing but empty fields, has no real test and fails. This shows the plan is well formed
  and extracts what its author says; it cannot show the live site looks like the recording (only a live run does). Stored arguments that
  broke an earlier version (P12) are not replayed, because no recorded page goes with them; the metrics say how many.
* **G4, host and step limits.** The plan is validated again, then inputs generated from the input schema, and a battery of hostile
  values for every argument a template uses (`..`, `@evil.example`, a second URL, whitespace, a right-to-left override, a very long
  string, a nested template), are rendered. A rendering must either be refused or still go only to approved hosts, with the same steps
  and the same selectors.
* **G6, the browser sandbox audit.** A browser container is started with no network and its hardening, as Docker reports it, must meet
  the same invariants as a code sandbox (`g6_containment.check_hardening`). The plan is not run and no decoy is planted. When there is no
  browser (no Docker, no image, no MiniLocker) the gate is `skipped` with the reason, never `passed`.

Nothing here runs package code, and nothing here opens a network connection. Proven by `tests/test_plan_gates.py`.
"""

from __future__ import annotations

import json
from typing import Any

from toolforge import schemacheck
from toolforge.adapters import browser_fixture, browser_plan
from toolforge.verify import schemagen
from toolforge.verify.pipeline import GateContext, GateResult, clip, failed, passed, skipped

HOSTILE_VALUES: tuple[str, ...] = (
    ".",
    "..",
    "../../etc/passwd",
    "@evil.example",
    "evil.example/",
    "//evil.example/x",
    "https://evil.example/x",
    "\\\\evil.example",
    "a b\tc",
    "‮gnp.exe",
    "?x=1&y=2#frag",
    "%2e%2e",
    "%0d%0aHost: evil.example",
    '"], input[type=password], a["',
    "{{other}}",
    "x" * 400,
)
GENERATED_INPUTS = 30


def is_plan(ctx: GateContext) -> bool:
    """True for a package of kind `browser_plan`. False for one whose manifest cannot be read (the code gates report that)."""
    try:
        return ctx.package.manifest.kind == browser_plan.PLAN_KIND
    except Exception:
        return False


def _plan(ctx: GateContext) -> tuple[dict[str, Any] | None, list[str]]:
    """The validated plan, or why it cannot be used."""
    plan, issues = browser_plan.parse_plan(ctx.package.files.get(browser_plan.PLAN_FILE, ""))
    if plan is not None:
        manifest = ctx.package.manifest
        issues = browser_plan.validate_plan(
            plan,
            hosts=manifest.permissions.network,
            arg_names=set(manifest.input_schema.get("properties") or {}),
        )
    if issues or plan is None:
        return None, [clip(f"{i.path}: {i.message}", 200) for i in issues[:8]]
    return plan, []


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# --- G3 ----------------------------------------------------------------------------------------------------------------------


def dry_run(ctx: GateContext) -> GateResult:
    manifest = ctx.package.manifest
    plan, why = _plan(ctx)
    if plan is None:
        return failed("the plan is not valid", why)
    hosts = tuple(manifest.permissions.network)
    checked = [
        (n, e) for n, e in enumerate(manifest.examples, start=1) if e.output is not None and not e.live
    ]
    live = sum(1 for e in manifest.examples if e.live)
    metrics: dict[str, Any] = {"examples_checked": 0, "examples_live_skipped": live}
    if ctx.regression_cases:
        metrics["regression_cases_not_run"] = len(ctx.regression_cases)
    if not checked:
        return failed(
            "a browser plan has no test: no example gives an expected output and is not live",
            [
                "add an example with an `output` and without `live`; the dry run replays it against fixtures/*.json",
                "a live example (`live: true`) only runs once, when the operator approves the hosts",
            ],
            metrics=metrics,
        )
    _, problems = browser_fixture.load_fixtures(ctx.package.files)
    if problems:
        return failed("the recorded pages are not usable", problems[:5], metrics=metrics)
    found_something = False
    for number, example in checked:
        # A fresh browser per example: no page carries over from one to the next.
        browser, _ = browser_fixture.load_fixtures(ctx.package.files)
        if browser is None:
            return failed(
                "the plan has no recorded page to run against",
                ["add fixtures/<name>.json with the pages the examples visit (forge_recipe 'browser-plan')"],
                metrics=metrics,
            )
        try:
            steps = browser_plan.render_plan(plan, example.input, hosts)
            got = {"fields": browser_plan.run_steps(steps, browser, hosts=hosts).fields}
        except browser_plan.PlanError as exc:
            return failed(
                f"example {number} cannot be rendered: {clip(exc.message, 120)}",
                ["check the example's input against the templates in plan.json"],
                metrics=metrics,
            )
        except browser_plan.PlanFailure as exc:
            return failed(
                f"example {number} failed in the dry run: {clip(exc.message, 140)}",
                [
                    "each page a goto or click leads to needs a recorded page with that exact address in fixtures/"
                ],
                metrics=metrics,
            )
        metrics["examples_checked"] += 1
        if _dump(got) != _dump(example.output):
            return failed(
                f"example {number} extracted something other than its output",
                [f"extracted: {clip(_dump(got), 180)}", f"expected: {clip(_dump(example.output), 180)}"],
                metrics=metrics,
            )
        found_something = found_something or any(got["fields"].values())
    if not found_something:
        return failed(
            "every example expects only empty fields, so the tests would pass for a plan that extracts nothing",
            ["record a page that has the content and give an example whose output holds it"],
            metrics=metrics,
        )
    count = metrics["examples_checked"]
    return passed(
        f"the plan ran against the recorded pages and {count} example{'s' if count != 1 else ''} matched "
        f"({live} live example{'s' if live != 1 else ''} not run: they need the real site)",
        metrics=metrics,
    )


# --- G4 ----------------------------------------------------------------------------------------------------------------------


def limits(ctx: GateContext) -> GateResult:
    manifest = ctx.package.manifest
    plan, why = _plan(ctx)
    if plan is None:
        return failed("the plan is not valid", why)
    hosts = tuple(manifest.permissions.network)
    steps = plan["steps"]
    gotos = sum(1 for s in steps if s["step"] == "goto")
    metrics: dict[str, Any] = {
        "steps": len(steps),
        "gotos": gotos,
        "inputs_rendered": 0,
        "hostile_rendered": 0,
        "refused": 0,
    }
    try:
        generated = schemagen.generate(
            manifest.input_schema, manifest.examples, seed=ctx.package.code_sha, count=GENERATED_INPUTS
        )
    except schemacheck.SchemaCheckError as exc:
        return failed(
            "the input schema could not be checked in time while generating inputs",
            [f"simplify the input schema ({clip(str(exc), 100)})"],
        )
    base = dict(manifest.examples[0].input) if manifest.examples else {}
    cases: list[tuple[dict[str, Any], str | None]] = [(g.args, None) for g in generated]
    cases += [
        ({**base, name: value}, name) for name in browser_plan.template_args(plan) for value in HOSTILE_VALUES
    ]
    shape = [(s["step"], s.get("selector")) for s in steps]
    for args, hostile in cases:
        try:
            rendered = browser_plan.render_plan(plan, args, hosts)
        except browser_plan.PlanError:
            metrics["refused"] += 1  # a refused rendering is a correct answer
            continue
        metrics["hostile_rendered" if hostile else "inputs_rendered"] += 1
        if [(s["step"], s.get("selector")) for s in rendered] != shape:
            return failed(
                "an argument changed the plan's steps or selectors",
                ["this is a ToolForge problem if you see it: arguments may fill text, never structure"],
                metrics=metrics,
            )
        for step in rendered:
            if step["step"] == "goto" and browser_plan.host_of(step["url"]) not in hosts:
                return failed(
                    "an argument moved a goto to a host that is not approved",
                    [
                        f"the argument {clip(hostile or '(generated input)', 40)} reached {clip(step['url'], 80)}"
                    ],
                    metrics=metrics,
                )
    if not metrics["inputs_rendered"] and not metrics["hostile_rendered"]:
        return failed(
            "no input could be turned into a runnable plan, so the plan can never run",
            ["make every argument a template uses a required string or number in input_schema"],
            metrics=metrics,
        )
    return passed(
        f"{metrics['inputs_rendered']} generated and {metrics['hostile_rendered']} hostile renderings stayed on approved hosts "
        f"({metrics['refused']} refused); {len(steps)} steps, {gotos} page load{'s' if gotos != 1 else ''}",
        metrics=metrics,
    )


# --- G6 ----------------------------------------------------------------------------------------------------------------------


def audit(ctx: GateContext) -> GateResult:
    from toolforge.verify.g6_containment import (
        check_hardening,
    )  # imported here: that module dispatches to this one

    again = [
        "build the browser image (docs/browser-plans.md), then submit the plan again with a higher version"
    ]
    if ctx.browser is None:
        return skipped(
            "the browser sandbox was not audited: none is configured here, so containment is not measured",
            again,
            metrics={"browser_audited": False},
        )
    result = ctx.browser.audit(ledger_path=ctx.ledger_path, label=f"verify:{ctx.label}:G6")
    if result.hardening is None:
        return skipped(
            f"the browser sandbox was not audited: {clip(result.reason, 180)}",
            again,
            metrics={"browser_audited": False},
        )
    wrong, unknown = check_hardening(result.hardening)
    if wrong:
        return failed(
            f"the browser sandbox is weaker than promised ({len(wrong)} problem{'s' if len(wrong) != 1 else ''})",
            [clip(item, 160) for item in wrong[:6]],
            metrics={"browser_audited": True},
        )
    if unknown:
        return skipped(
            f"the browser sandbox did not report: {clip(', '.join(unknown), 160)}",
            ["run `toolforge doctor`"],
            metrics={"browser_audited": False},
        )
    return passed(
        "the browser sandbox is non-root and read-only with no capabilities and, for this audit, no network; "
        "the plan itself was not run",
        metrics={"browser_audited": True},
    )


__all__ = ["HOSTILE_VALUES", "audit", "dry_run", "is_plan", "limits"]
