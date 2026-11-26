"""Gates G3, G4, G5 and G6 for a browser plan (P14b), and the whole pipeline up to the approval request.

Recorded pages and a scripted browser audit: no sandbox is opened, nothing leaves the machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from plan_helpers import HARDENED, HOST, OUTPUT, PAGE, ScriptedRunner, plan_package
from verify_helpers import MemoryLedgers

from toolforge.adapters import browser_plan
from toolforge.config import Settings
from toolforge.registry import Registry, lifecycle
from toolforge.sandbox import FakeRunner
from toolforge.services import approvals
from toolforge.verify import plan_gates
from toolforge.verify.g3_selftest import SelfTestGate
from toolforge.verify.g4_fuzz import FuzzGate
from toolforge.verify.g6_containment import ContainmentGate
from toolforge.verify.pipeline import GateContext, Pipeline


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def context(settings: Settings, browser: Any = None, **changes: Any) -> GateContext:
    return GateContext(
        package=plan_package(**changes), settings=settings, browser=browser, label="weather_news@1.0.0"
    )


def example(output: Any = OUTPUT, topic: str = "weather", **more: Any) -> dict[str, Any]:
    return {"input": {"topic": topic}, **({"output": output} if output is not None else {}), **more}


# --- G3 ----------------------------------------------------------------------------------------------------------------------


def test_g3_runs_the_plan_against_the_recorded_pages(settings: Settings) -> None:
    result = plan_gates.dry_run(context(settings))
    assert result.status == "passed", result.summary
    assert result.metrics == {"examples_checked": 1, "examples_live_skipped": 0}


def test_g3_counts_what_it_could_not_run(settings: Settings) -> None:
    ctx = context(settings, examples=[example(), example(live=True)])
    ctx.regression_cases = ({"topic": "old"},)
    result = plan_gates.dry_run(ctx)
    assert result.status == "passed"
    assert result.metrics["examples_live_skipped"] == 1 and result.metrics["regression_cases_not_run"] == 1


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        ({"examples": [example(None)]}, "has no test"),
        ({"examples": [example(live=True)]}, "has no test"),
        ({"examples": [example({"fields": {"headlines": ["Other"]}})]}, "something other than its output"),
        ({"examples": [example({"fields": {}})]}, "something other than its output"),
        ({"examples": [example(topic="snow")]}, "failed in the dry run"),
        ({"examples": [example(topic="..")]}, "cannot be rendered"),
        ({"files": {"fixtures/front.json": "{"}}, "not usable"),
        ({"pages": []}, "no recorded page"),
        ({"pages": [{**PAGE, "text": "nothing"}]}, "failed in the dry run"),
        (
            {"pages": [{**PAGE, "text": "Top\n"}], "examples": [example({"fields": {"headlines": []}})]},
            "only empty fields",
        ),
        ({"plan": {"steps": []}}, "not valid"),
    ],
)
def test_g3_fails_a_plan_whose_examples_prove_nothing(settings: Settings, changes: dict, needle: str) -> None:
    result = plan_gates.dry_run(context(settings, **changes))
    assert result.status == "failed" and needle in result.summary, result.summary
    assert result.hints


def test_a_page_never_carries_over_between_examples(settings: Settings) -> None:
    two = [example(), example()]
    assert plan_gates.dry_run(context(settings, examples=two)).metrics["examples_checked"] == 2


# --- G4 ----------------------------------------------------------------------------------------------------------------------


def test_g4_tries_every_hostile_value_in_every_templated_argument(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    two_args = {
        "steps": [
            {"step": "goto", "url": f"https://{HOST}/topic/{{{{topic}}}}"},
            {"step": "extract_text", "field": "headlines", "after": "{{anchor}}"},
        ]
    }
    schema = {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "anchor": {"type": "string"},
            "unused": {"type": "string"},
        },
        "required": ["topic", "anchor"],
        "additionalProperties": False,
    }
    seen: list[dict[str, Any]] = []
    real = browser_plan.render_plan
    monkeypatch.setattr(browser_plan, "render_plan", lambda p, a, h: seen.append(dict(a)) or real(p, a, h))
    ctx = context(
        settings,
        plan=two_args,
        input_schema=schema,
        examples=[{"input": {"topic": "weather", "anchor": "Top"}}],
    )
    result = plan_gates.limits(ctx)
    assert result.status == "passed", result.summary
    for name in ("topic", "anchor"):
        tried = {a[name] for a in seen}
        assert set(plan_gates.HOSTILE_VALUES) <= tried, name
    assert not any(a.get("unused") in plan_gates.HOSTILE_VALUES for a in seen)
    metrics = result.metrics
    assert (
        metrics["hostile_rendered"] > 0
        and metrics["refused"] > 0
        and metrics["steps"] == 2
        and metrics["gotos"] == 1
    )


def test_g4_fails_if_an_argument_could_move_a_goto(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def careless(plan: Any, args: Any, hosts: Any) -> list[dict[str, Any]]:
        return [{"step": "goto", "url": f"https://{args.get('topic')}"}, plan["steps"][1]]

    monkeypatch.setattr(browser_plan, "render_plan", careless)
    result = plan_gates.limits(context(settings))
    assert result.status == "failed" and "not approved" in result.summary


def test_g4_fails_if_an_argument_could_change_a_selector(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = browser_plan.render_plan

    def careless(plan: Any, args: Any, hosts: Any) -> list[dict[str, Any]]:
        steps = real(plan, args, hosts)
        return [*steps, {"step": "click", "selector": str(args["topic"])}]

    monkeypatch.setattr(browser_plan, "render_plan", careless)
    result = plan_gates.limits(context(settings))
    assert result.status == "failed" and "steps or selectors" in result.summary


def test_g4_fails_a_plan_that_can_never_run(settings: Settings) -> None:
    schema = {
        "type": "object",
        "properties": {"topic": {"type": "array", "items": {"type": "string"}}},
        "required": ["topic"],
        "additionalProperties": False,
    }
    ctx = context(settings, input_schema=schema, examples=[{"input": {"topic": []}}])
    monkey = plan_gates.HOSTILE_VALUES
    try:
        plan_gates.HOSTILE_VALUES = ()
        result = plan_gates.limits(ctx)
    finally:
        plan_gates.HOSTILE_VALUES = monkey
    assert result.status == "failed" and "never run" in result.summary


def test_g4_refuses_an_invalid_plan(settings: Settings) -> None:
    assert plan_gates.limits(context(settings, plan={"steps": []})).status == "failed"


# --- The gates dispatch to the plan rules ------------------------------------------------------------------------------------


def test_the_code_gates_hand_a_plan_to_the_plan_rules_without_a_sandbox(settings: Settings) -> None:
    ctx = context(settings, ScriptedRunner())
    assert ctx.runner is None and ctx.blob is None
    for gate in (SelfTestGate(), FuzzGate(), ContainmentGate()):
        assert gate.run(ctx).status == "passed", gate.id


# --- G6 ----------------------------------------------------------------------------------------------------------------------


def test_g6_without_a_browser_is_skipped_never_passed(settings: Settings) -> None:
    for browser in (None, ScriptedRunner(ready=False)):
        result = plan_gates.audit(context(settings, browser))
        assert result.status == "skipped" and result.metrics == {"browser_audited": False}


def test_g6_audits_the_browser_container_and_says_the_plan_did_not_run(settings: Settings) -> None:
    browser = ScriptedRunner()
    result = plan_gates.audit(context(settings, browser))
    assert result.status == "passed" and "was not run" in result.summary
    assert browser.runs == [] and len(browser.audits) == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("user", "0"),
        ("read_only_rootfs", False),
        ("cap_drop", []),
        ("cap_add", ["NET_ADMIN"]),
        ("privileged", True),
        ("security_opt", []),
        ("network_mode", "bridge"),
        ("binds", ["/:/host"]),
        ("pid_mode", "host"),
        ("ipc_mode", "host"),
    ],
)
def test_g6_fails_a_weaker_browser_container(settings: Settings, key: str, value: Any) -> None:
    result = plan_gates.audit(context(settings, ScriptedRunner(hardening={**HARDENED, key: value})))
    assert result.status == "failed" and "weaker than promised" in result.summary


def test_g6_skips_when_docker_does_not_say(settings: Settings) -> None:
    partial = {key: value for key, value in HARDENED.items() if key != "privileged"}
    result = plan_gates.audit(context(settings, ScriptedRunner(hardening=partial)))
    assert result.status == "skipped" and "privileged" in result.summary


# --- The whole pipeline ------------------------------------------------------------------------------------------------------


def verify(settings: Settings, registry: Registry, browser: Any, **changes: Any):
    sandbox = FakeRunner()
    ledgers = MemoryLedgers()
    pipeline = Pipeline(settings, sandbox, registry, ledger_factory=ledgers, browser=browser)
    report = pipeline.run(plan_package(**changes), "test")
    assert sandbox.handles == [], "a plan opens no code sandbox"
    return report


def test_a_good_plan_ends_waiting_for_approval_and_is_never_fully_verified(settings: Settings) -> None:
    with Registry(settings.home) as registry:
        report = verify(settings, registry, ScriptedRunner())
        statuses = {g.id: g.status for g in report.gates}
        assert statuses == {
            "G1": "passed",
            "G2": "passed",
            "G3": "passed",
            "G4": "passed",
            "G5": "skipped",
            "G6": "passed",
            "G7": statuses.get("G7"),
        } or statuses == {
            "G1": "passed",
            "G2": "passed",
            "G3": "passed",
            "G4": "passed",
            "G5": "skipped",
            "G6": "passed",
        }
        assert report.status == lifecycle.PENDING_APPROVAL, report.text()
        assert "does not review browser plans" in next(g for g in report.gates if g.id == "G5").summary


def test_a_secret_written_into_a_plan_is_found(settings: Settings) -> None:
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    plan = {
        "steps": [
            {"step": "goto", "url": f"https://{HOST}/topic/{{{{topic}}}}"},
            {"step": "type", "selector": "input", "text": token},
            {"step": "extract_text", "field": "headlines", "as": "lines", "after": "Top"},
        ]
    }
    with Registry(settings.home) as registry:
        report = verify(settings, registry, None, plan=plan)
        assert {g.id: g.status for g in report.gates}["G1"] == "failed"
        assert token not in report.text()


def test_the_approval_request_shows_the_operator_every_step(settings: Settings) -> None:
    with Registry(settings.home) as registry:
        report = verify(settings, registry, None)
        assert report.status == lifecycle.PENDING_APPROVAL, report.text()
        approval = registry.get_approval(report.approval["id"])
        doc = approval.requested
        assert doc["skipped_gates"] == ["G5", "G6"]
        assert [line.split()[1] for line in doc["plan"]] == ["goto", "extract_text"]
        assert f"https://{HOST}/topic/{{{{topic}}}}" in doc["plan"][0]
        assert set(approvals.BROWSER_LIMITS) <= set(doc["limits"])
        text = approvals.render_text(approval)
        assert "browser plan steps:" in text and doc["plan"][1] in text
        json.dumps(doc)


def test_an_unreadable_plan_is_said_to_be_unreadable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Registry(settings.home) as registry:
        verify(settings, registry, None)
        record = registry.find_version("weather_news", "1.0.0")
        monkeypatch.setattr(registry.blobs, "get", lambda sha: b"broken")
        (line,) = approvals.plan_lines(registry, record)
        assert "could not be read" in line and "do not approve" in line


def test_a_weak_browser_container_rejects_the_plan(settings: Settings) -> None:
    with Registry(settings.home) as registry:
        report = verify(settings, registry, ScriptedRunner(hardening={**HARDENED, "privileged": True}))
        assert report.status == lifecycle.REJECTED
