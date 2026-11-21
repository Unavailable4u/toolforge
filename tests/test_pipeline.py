"""The verification pipeline with scripted gates and a real registry: order, stopping, skipping, rows, events, budgets, failure modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from verify_helpers import MemoryLedgers, ScriptedGate, valid_package

from toolforge.config import Settings
from toolforge.errors import SandboxUnavailable
from toolforge.registry import Registry, lifecycle
from toolforge.verify import pipeline
from toolforge.verify.pipeline import (
    GATE_CLASS_PATHS,
    GATE_INFO,
    PIPELINE_GATE_IDS,
    GateResult,
    Pipeline,
    clip,
    default_gates,
    failed,
    gates_markdown,
    passed,
    report_from_registry,
    skipped,
)


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def gates(*ids: str, **overrides: ScriptedGate) -> list[ScriptedGate]:
    return [overrides.get(g) or ScriptedGate(g) for g in ids]


def build(settings, registry, gate_list, ledgers=None, **kw) -> Pipeline:
    return Pipeline(settings, None, registry, ledger_factory=ledgers, gates=gate_list, **kw)


# --- GATE_INFO -------------------------------------------------------------------------------------------------------------------


def test_gate_info_lists_g1_to_g7_in_order() -> None:
    assert [g.id for g in GATE_INFO] == ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    assert all(g.name and g.plain_summary and g.catches and g.example for g in GATE_INFO)


def test_status_is_built_exactly_when_a_gate_class_is_registered() -> None:
    assert (
        {g.id for g in GATE_INFO if g.status == "built"} == set(GATE_CLASS_PATHS) == {"G1", "G2", "G3", "G4"}
    )
    assert {g.id for g in GATE_INFO if g.status == "planned"} == {"G5", "G6", "G7"}


def test_every_registered_gate_class_exists_and_has_the_interface() -> None:
    built = default_gates()
    assert [g.id for g in built] == ["G1", "G2", "G3", "G4"]
    assert all(isinstance(g.budget_s, int) and g.budget_s > 0 and callable(g.run) for g in built)


def test_fuzz_inputs_reach_only_the_gate_that_takes_them() -> None:
    built = {g.id: g for g in default_gates(fuzz_inputs=7)}
    assert built["G4"].count == 7


def test_the_gate_table_lists_every_gate() -> None:
    table = gates_markdown()
    assert all(f"| {g.id} | {g.name} | {g.status} |" in table for g in GATE_INFO)


# --- Results ---------------------------------------------------------------------------------------------------------------------


def test_a_result_cleans_and_limits_its_text() -> None:
    result = failed("x" * 1000, ["a\x1b[31mred\x00" + "y" * 1000] * 40)
    assert len(result.summary) <= pipeline.SUMMARY_CHARS and len(result.hints) == pipeline.MAX_HINTS
    assert all(len(h) <= pipeline.HINT_CHARS and "\x1b" not in h and "\x00" not in h for h in result.hints)


def test_an_unknown_status_is_refused() -> None:
    with pytest.raises(ValueError):
        GateResult("maybe", "no")


def test_clip_accepts_anything() -> None:
    assert clip(None) == "None" and clip(b"\xff") != "" and len(clip("z" * 5000, 50)) <= 50


# --- Order, stopping, status ------------------------------------------------------------------------------------------------------


def test_g1_to_g4_passing_is_verified_basic_with_g5_and_g6_skipped(settings, registry) -> None:
    report = build(settings, registry, gates("G1", "G2", "G3", "G4")).run(valid_package(), "agent")
    assert report.status == lifecycle.VERIFIED_BASIC and report.ok
    assert [(g.id, g.status) for g in report.gates] == [
        ("G1", "passed"), ("G2", "passed"), ("G3", "passed"), ("G4", "passed"), ("G5", "skipped"), ("G6", "skipped"),
    ]  # fmt: skip
    assert "not built" in report.gates[4].summary
    assert registry.get_version(report.version_id).status == lifecycle.VERIFIED_BASIC


def test_all_six_gates_passing_is_verified(settings, registry) -> None:
    report = build(settings, registry, gates(*PIPELINE_GATE_IDS)).run(valid_package(), "agent")
    assert report.status == lifecycle.VERIFIED


def test_a_gate_that_skips_caps_the_status(settings, registry) -> None:
    custom = gates(*PIPELINE_GATE_IDS, G4=ScriptedGate("G4", skipped("nothing to fuzz")))
    assert build(settings, registry, custom).run(valid_package(), "agent").status == lifecycle.VERIFIED_BASIC


def test_gates_run_in_order_and_the_first_failure_stops_the_run(settings, registry) -> None:
    g = gates("G1", "G2", "G3", "G4", G2=ScriptedGate("G2", failed("the build broke", ["do this"])))
    report = build(settings, registry, g).run(valid_package(), "agent")
    assert report.status == lifecycle.REJECTED and not report.ok
    assert [x.status for x in report.gates] == [
        "passed",
        "failed",
        "not_run",
        "not_run",
        "not_run",
        "not_run",
    ]
    assert [x.calls for x in g] == [1, 1, 0, 0]
    assert (
        "G2" in report.next_action
        and "do this" in report.next_action
        and "higher version" in report.next_action
    )
    assert [row.gate for row in registry.gate_results(report.version_id)] == ["G1", "G2"]


def test_stored_rows_keep_summary_hints_metrics_and_detail(settings, registry) -> None:
    result = passed("fine", metrics={"n": 3}, detail={"extra": [1]})
    result.hints = ["h"]
    report = build(settings, registry, [ScriptedGate("G1", result)]).run(valid_package(), "agent")
    row = registry.gate_results(report.version_id)[0]
    assert row.status == "passed"
    assert row.detail == {"summary": "fine", "hints": ["h"], "metrics": {"n": 3}, "extra": [1]}


def test_a_rejection_records_the_reason_in_the_event(settings, registry) -> None:
    ledgers = MemoryLedgers()
    g = gates("G1", G1=ScriptedGate("G1", failed("secret found")))
    report = build(settings, registry, g, ledgers).run(valid_package(), "agent")
    assert report.status == "rejected"
    rejected = [p for _, _, t, p in ledgers.events if t == "forge.rejected"]
    assert rejected and "G1: secret found" in json.dumps(rejected[0])


# --- Events ----------------------------------------------------------------------------------------------------------------------


def test_the_events_follow_the_run(settings, registry, tmp_path) -> None:
    ledgers = MemoryLedgers()
    report = build(settings, registry, gates("G1", "G2", "G3", "G4"), ledgers).run(valid_package(), "agent")
    assert ledgers.types == [
        "forge.submitted", "forge.verifying",
        "forge.gate.started", "forge.gate.passed",
        "forge.gate.started", "forge.gate.passed",
        "forge.gate.started", "forge.gate.passed",
        "forge.gate.started", "forge.gate.passed",
        "forge.gate.skipped", "forge.gate.skipped",
        "forge.verified",
    ]  # fmt: skip
    submitted = ledgers.events[0][3]
    assert (
        submitted["submitted_by"] == "agent"
        and submitted["code_sha"]
        and submitted["permissions"]["network_hosts"] == 0
    )
    assert all(actor == "forge" for _, actor, _, _ in ledgers.events)
    assert {task for task, *_ in ledgers.events} == {f"verify:my_tool@0.1.0:{report.run_id}"}
    assert all(str(p).endswith(f"verify-{report.run_id}.jsonl") for p in ledgers.paths)


def test_events_never_carry_source_text(settings, registry) -> None:
    ledgers = MemoryLedgers()
    package = valid_package(tool='def run(args):\n    return {"text": "MARKER_SOURCE_LINE", "words": 0}\n')
    build(settings, registry, gates("G1"), ledgers).run(package, "agent")
    assert "MARKER_SOURCE_LINE" not in json.dumps(ledgers.events)


def test_a_dry_run_writes_nothing(settings, registry) -> None:
    ledgers = MemoryLedgers()
    pipe = Pipeline(settings, None, None, ledger_factory=ledgers, gates=gates("G1", "G2"))
    report = pipe.run(valid_package(), "agent")
    assert report.dry_run and not report.registered and report.version_id is None
    assert registry.list_versions() == [] and ledgers.events == []
    assert report.status == lifecycle.VERIFIED_BASIC


# --- Failure modes ---------------------------------------------------------------------------------------------------------------


def test_a_gate_that_raises_is_a_failed_gate_and_says_nothing_about_why(settings, registry) -> None:
    g = gates("G1", "G2", G1=ScriptedGate("G1", raises=RuntimeError("SECRET_INTERNAL_DETAIL /root/x")))
    report = build(settings, registry, g).run(valid_package(), "agent")
    assert report.status == "rejected" and report.gates[0].status == "failed"
    assert "RuntimeError" in report.gates[0].summary
    assert "SECRET_INTERNAL_DETAIL" not in json.dumps(report.as_dict())
    assert g[1].calls == 0


def test_a_gate_that_exceeds_its_budget_fails(settings, registry) -> None:
    ticks = iter(range(0, 10_000, 100))
    g = [ScriptedGate("G1", budget_s=30)]
    report = build(settings, registry, g, clock=lambda: float(next(ticks))).run(valid_package(), "agent")
    assert report.gates[0].status == "failed" and "limit is 30" in report.gates[0].summary


def test_a_slow_gate_that_already_failed_keeps_its_own_message(settings, registry) -> None:
    ticks = iter(range(0, 10_000, 100))
    g = [ScriptedGate("G1", failed("real problem"), budget_s=30)]
    report = build(settings, registry, g, clock=lambda: float(next(ticks))).run(valid_package(), "agent")
    assert report.gates[0].summary == "real problem"


def test_a_gate_that_changes_the_package_is_failed(settings, registry) -> None:
    def tamper(ctx) -> None:
        ctx.package.files["tool.py"] = ctx.package.files["tool.py"] + "\n# changed\n"

    g = [ScriptedGate("G1", action=tamper), ScriptedGate("G2")]
    report = build(settings, registry, g).run(valid_package(), "agent")
    assert report.status == "rejected" and "changed the package" in report.gates[0].summary
    assert g[1].calls == 0


def test_a_gate_that_returns_the_wrong_thing_is_failed(settings, registry) -> None:
    class Bad(ScriptedGate):
        def run(self, ctx):  # type: ignore[no-untyped-def]
            return {"status": "passed"}

    report = build(settings, registry, [Bad("G1")]).run(valid_package(), "agent")
    assert report.gates[0].status == "failed"


def test_an_unavailable_sandbox_is_raised_and_the_version_waits(settings, registry) -> None:
    ledgers = MemoryLedgers()
    boom = SandboxUnavailable("no docker", code="docker_unavailable", hint="start it")
    g = gates("G1", "G2", G2=ScriptedGate("G2", raises=boom))
    package = valid_package()
    with pytest.raises(SandboxUnavailable):
        build(settings, registry, g, ledgers).run(package, "agent")
    record = registry.find_version("my_tool", "0.1.0")
    assert record.status == lifecycle.VERIFYING
    assert [r.gate for r in registry.gate_results(record.id)] == ["G1"]
    assert "forge.verification.aborted" in ledgers.types
    # the same content again resumes instead of starting over
    again = build(settings, registry, gates("G1", "G2", "G3", "G4"), ledgers).run(
        package, "agent", version=record
    )
    assert again.status == lifecycle.VERIFIED_BASIC and again.version_id == record.id
    assert ledgers.types.count("forge.verifying") == 1
    assert [e[3]["resumed"] for e in ledgers.events if e[2] == "forge.submitted"] == [False, True]


def test_an_invalid_package_is_rejected_without_being_registered(settings, registry) -> None:
    package = valid_package(description="short")  # too short to validate
    assert not package.validate(settings).ok
    g = gates("G1", G1=ScriptedGate("G1", failed("the manifest is invalid", ["fix it"])))
    report = build(settings, registry, g).run(package, "agent")
    assert report.status == "rejected" and not report.registered and not report.dry_run
    assert registry.list_versions() == []
    assert "submit it again" in report.next_action and "higher version" not in report.next_action


# --- Reports ---------------------------------------------------------------------------------------------------------------------


def test_the_report_renders_as_json_and_text(settings, registry) -> None:
    report = build(settings, registry, gates("G1", "G2")).run(valid_package(), "agent")
    data = json.loads(json.dumps(report.as_dict()))
    assert data["tool"] == "my_tool" and data["status"] == "verified_basic" and len(data["gates"]) == 6
    text = report.text()
    assert (
        "my_tool@0.1.0: verified_basic" in text
        and "G1 Static check: passed" in text
        and text.endswith(report.next_action)
    )


def test_a_stored_report_can_be_rebuilt(settings, registry) -> None:
    g = gates("G1", "G2", G2=ScriptedGate("G2", failed("broke", ["fix the build"])))
    first = build(settings, registry, g).run(valid_package(), "agent")
    again = report_from_registry(registry, registry.get_version(first.version_id))
    assert again.status == "rejected"
    assert [(x.id, x.status, x.summary) for x in again.gates[:2]] == [
        (x.id, x.status, x.summary) for x in first.gates[:2]
    ]
    assert again.gates[1].hints == ("fix the build",)
    assert "G2" in again.next_action


def test_warnings_from_gates_are_collected(settings, registry) -> None:
    result = passed("fine", detail={"warnings": ["tool.py:3 reads os.environ", 5]})
    report = build(settings, registry, [ScriptedGate("G1", result)]).run(valid_package(), "agent")
    assert report.warnings == ("tool.py:3 reads os.environ",)


# --- P07: progress reports -------------------------------------------------------------------------------------------------------------


def test_progress_is_reported_around_every_gate_and_never_changes_the_result(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    seen: list[tuple[str, str, str | None]] = []
    gates = [ScriptedGate("G1"), ScriptedGate("G2", pipeline.failed("no build", ["fix"])), ScriptedGate("G3")]
    plain = pipeline.Pipeline(settings, None, None, gates=gates).run(valid_package(), "a")
    reported = pipeline.Pipeline(
        settings,
        None,
        None,
        gates=[
            ScriptedGate("G1"),
            ScriptedGate("G2", pipeline.failed("no build", ["fix"])),
            ScriptedGate("G3"),
        ],
    ).run(valid_package(), "a", progress=lambda gate, phase, status: seen.append((gate, phase, status)))
    assert (reported.status, [g.status for g in reported.gates]) == (
        plain.status,
        [g.status for g in plain.gates],
    )
    assert seen[:4] == [
        ("G1", "started", None),
        ("G1", "finished", "passed"),
        ("G2", "started", None),
        ("G2", "finished", "failed"),
    ]


def test_a_progress_callback_that_raises_is_ignored(tmp_path: Path) -> None:
    def broken(gate: str, phase: str, status: str | None) -> None:
        raise RuntimeError("the client went away")

    settings = Settings(home=tmp_path / "home")
    report = pipeline.Pipeline(
        settings, None, None, gates=[ScriptedGate(g) for g in ("G1", "G2", "G3", "G4")]
    ).run(valid_package(), "a", progress=broken)
    assert report.status == lifecycle.VERIFIED_BASIC
