"""The attack runner: how a driver's outcome becomes a verdict, the checks every attack gets, isolation between attacks, and the report."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from toolforge import cli_run
from toolforge.attacks import catalog, runner
from toolforge.attacks.catalog import (
    BREACHED,
    CONTAINED,
    FAIL,
    INCONCLUSIVE,
    PASS,
    SKIP,
    SKIPPED,
    Attack,
    Check,
    ok,
)
from toolforge.attacks.runner import AttackResult, Lab, run_attacks, run_one
from toolforge.errors import SandboxError, SandboxUnavailable, ToolForgeError


@pytest.fixture
def lab(tmp_path: Path, minilocker: None) -> Iterator[Lab]:
    home = tmp_path / "home"
    home.mkdir()
    made = Lab(home, docker_ok=False)
    yield made
    made.close()


def attack(
    drive: Any = None, *, needs_docker: bool = False, tampers: bool = False, name: str = "fake"
) -> Attack:
    return Attack(
        name,
        "A fake attack",
        "Does something.",
        "Is stopped.",
        drive or (lambda lab: {"seen": True}),
        (Check("it was seen", lambda o: ok("seen") if o.get("seen") else catalog.unproven("not seen")),),
        needs_docker=needs_docker,
        tampers_ledger=tampers,
    )


def write_ledger(lab: Lab, folder: str, events: list[tuple[str, dict[str, Any]]]) -> Path:
    path = lab.home / "ledgers" / folder / "verify-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    sink = cli_run.make_ledger(str(path), "test")
    for kind, payload in events:
        sink.append("forge", kind, payload)
    return path


# --- From a driver's outcome to a verdict -----------------------------------------------------------------------------------


def test_a_clean_run_is_contained(lab: Lab) -> None:
    result = run_one(attack(), lab, deadline_s=10)
    assert result.verdict == CONTAINED and "held" in result.evidence
    assert [c.scope for c in result.checks].count("always") == 3


def test_an_attack_that_needs_docker_is_skipped_not_contained(lab: Lab) -> None:
    called: list[int] = []
    result = run_one(
        attack(lambda lab: called.append(1) or {"seen": True}, needs_docker=True), lab, deadline_s=10
    )
    assert result.verdict == SKIPPED and "Docker" in result.evidence
    assert called == [], "a skipped attack must not even start"


def test_missing_minilocker_skips_every_attack(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner._ml_path, "minilocker_available", lambda: False)
    result = run_one(attack(), lab, deadline_s=10)
    assert result.verdict == SKIPPED and "submodule" in result.evidence


def test_a_driver_that_raises_is_inconclusive_with_the_reason(lab: Lab) -> None:
    def boom(lab: Lab) -> dict[str, Any]:
        raise KeyError("nope")

    result = run_one(attack(boom), lab, deadline_s=10)
    assert result.verdict == INCONCLUSIVE and "KeyError" in result.evidence


def test_an_unavailable_sandbox_mid_attack_is_skipped(lab: Lab) -> None:
    def gone(lab: Lab) -> dict[str, Any]:
        raise SandboxUnavailable("the daemon went away", hint="start it")

    result = run_one(attack(gone), lab, deadline_s=10)
    assert result.verdict == SKIPPED and "daemon went away" in result.evidence


def test_a_sandbox_failure_is_inconclusive(lab: Lab) -> None:
    def broken(lab: Lab) -> dict[str, Any]:
        raise SandboxError("could not start", hint="check the image")

    result = run_one(attack(broken), lab, deadline_s=10)
    assert result.verdict == INCONCLUSIVE and "could not start" in result.evidence


def test_an_attack_past_its_deadline_is_inconclusive_and_does_not_block_the_run(lab: Lab) -> None:
    started = time.monotonic()
    result = run_one(attack(lambda lab: time.sleep(5) or {"seen": True}), lab, deadline_s=0.3)
    assert result.verdict == INCONCLUSIVE and "deadline" in result.evidence
    assert time.monotonic() - started < 3


def test_cleanup_runs_after_a_driver_fails(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    cleaned: list[int] = []
    monkeypatch.setattr(lab, "cleanup", lambda: cleaned.append(1))

    def boom(lab: Lab) -> dict[str, Any]:
        raise RuntimeError("x")

    run_one(attack(boom), lab, deadline_s=10)
    assert cleaned


# --- The checks every attack gets ----------------------------------------------------------------------------------------------


def test_the_canary_check_passes_when_nothing_leaked(lab: Lab) -> None:
    assert lab._canary_check().status == PASS


def test_a_host_secret_in_a_ledger_is_a_breach_even_if_the_attack_checks_pass(lab: Lab) -> None:
    folder = lab.home / "ledgers" / "x@1"
    folder.mkdir(parents=True)
    (folder / "verify-1.jsonl").write_text(f'{{"note": "{lab.canary_token}"}}\n', encoding="utf-8")
    assert lab._canary_check().status == FAIL
    result = run_one(attack(), lab, deadline_s=10)
    assert result.verdict == BREACHED and "canary" in result.evidence


def test_a_changed_canary_file_is_a_breach(lab: Lab) -> None:
    lab.canary_path.write_text("something else", encoding="utf-8")
    assert lab._canary_check().status == FAIL


def test_a_removed_canary_variable_is_a_breach(lab: Lab) -> None:
    os.environ.pop(lab.canary_env)
    assert lab._canary_check().status == FAIL


def test_the_canary_variable_is_not_visible_after_the_lab_closes(tmp_path: Path, minilocker: None) -> None:
    made = Lab(tmp_path, docker_ok=False)
    name = made.canary_env
    assert os.environ[name] == made.canary_token
    made.close()
    assert name not in os.environ and not made.canary_path.exists()


def test_intact_ledgers_pass_and_an_edited_one_fails(lab: Lab) -> None:
    path = write_ledger(lab, "x@1", [("a.one", {"n": 1}), ("a.two", {"n": 2}), ("a.three", {"n": 3})])
    assert lab._chains_check(attack()).status == PASS
    assert lab.tamper(path)
    check = lab._chains_check(attack())
    assert check.status == FAIL and "verify-1.jsonl" in check.detail


def test_the_chain_check_is_skipped_for_the_attack_that_edits_a_ledger(lab: Lab) -> None:
    path = write_ledger(lab, "x@1", [("a.one", {}), ("a.two", {}), ("a.three", {})])
    lab.tamper(path)
    assert lab._chains_check(attack(tampers=True)).status == SKIP


def test_no_ledger_means_the_chain_check_is_skipped(lab: Lab) -> None:
    assert lab._chains_check(attack()).status == SKIP


def test_a_sandbox_never_destroyed_is_a_breach(lab: Lab) -> None:
    write_ledger(lab, "x@1", [("sandbox.created", {"name": "box-1"}), ("sandbox.audit", {})])
    check = lab._destroyed_check()
    assert check.status == FAIL and "box-1" in check.detail


def test_a_sandbox_created_and_destroyed_passes(lab: Lab) -> None:
    write_ledger(
        lab, "x@1", [("sandbox.created", {"name": "box-1"}), ("sandbox.destroyed", {"name": "box-1"})]
    )
    assert lab._destroyed_check().status == PASS


def test_no_sandbox_at_all_is_skipped_not_passed(lab: Lab) -> None:
    assert lab._destroyed_check().status == SKIP


def test_a_container_found_left_behind_is_a_breach(lab: Lab) -> None:
    write_ledger(
        lab, "x@1", [("sandbox.created", {"name": "box-1"}), ("sandbox.destroyed", {"name": "box-1"})]
    )
    lab.leftovers.append("minilocker-orphan")
    check = lab._destroyed_check()
    assert check.status == FAIL and "minilocker-orphan" in check.detail


def test_leftover_containers_are_only_looked_for_with_a_real_runner(lab: Lab) -> None:
    assert (
        lab.remove_leftover_containers() == []
    )  # no Docker here, and a stand-in runner never creates containers


def test_sandbox_facts_read_docker_report_from_events() -> None:
    events = [
        {
            "type": "sandbox.created",
            "payload": {"name": "a", "label": "verify:x:G6", "hardening": {"network_mode": "none"}},
        },
        {"type": "canary.seeded", "payload": {"items": [1, 2, 3]}},
        {"type": "canary.read", "payload": {}},
        {"type": "egress.blocked", "payload": {}},
        {"type": "sandbox.destroyed", "payload": {"name": "a"}},
    ]
    facts = Lab.sandbox_facts(events)
    assert facts["created"] == [{"name": "a", "label": "verify:x:G6", "network_mode": "none"}]
    assert facts["destroyed"] == ["a"] and facts["egress_events"] == 1
    assert facts["decoy_events"] == ["canary.read"] and facts["decoys_planted"] == 3


def test_audit_view_reads_stats_and_file_system_and_is_empty_without_an_audit() -> None:
    assert Lab.audit_view([]) == {}
    events = [
        {
            "type": "sandbox.audit",
            "payload": {
                "stats": {
                    "peak_pids": 9,
                    "pids_limit": 128,
                    "peak_memory_bytes": 5,
                    "memory_limit_bytes": 10,
                    "oom_killed": False,
                },
                "fs": {"rootfs_changes": 0, "workspace": {"bytes": 7}, "tmp_bytes": 3},
            },
        }
    ]
    view = Lab.audit_view(events)
    assert view["peak_pids"] == 9 and view["pids_limit"] == 128 and view["oom_killed"] is False
    assert view["workspace_bytes"] == 7 and view["tmp_bytes"] == 3 and view["rootfs_changes"] == 0


# --- Running the catalogue -------------------------------------------------------------------------------------------------------


def factory(homes: list[Path]) -> Any:
    def make(home: Path) -> Lab:
        homes.append(home)
        return Lab(home, docker_ok=False)

    return make


def test_an_unknown_attack_id_is_refused_before_anything_runs() -> None:
    with pytest.raises(ToolForgeError) as raised:
        run_attacks(["decoy_read", "no_such_attack"])
    assert raised.value.code == "unknown_attack" and "no_such_attack" in str(raised.value)


def test_every_attack_gets_its_own_home_that_is_deleted_afterwards(
    monkeypatch: pytest.MonkeyPatch, minilocker: None
) -> None:
    seen: list[str] = []

    def drive(lab: Lab) -> dict[str, Any]:
        seen.append(os.environ["TOOLFORGE_HOME"])
        assert Path(os.environ["TOOLFORGE_HOME"]) == lab.home
        return {"seen": True}

    monkeypatch.setattr(catalog, "ATTACKS", (attack(drive, name="one"), attack(drive, name="two")))
    homes: list[Path] = []
    results = run_attacks(None, lab_factory=factory(homes), deadline_s=10)
    assert [r.id for r in results] == ["one", "two"] and all(r.verdict == CONTAINED for r in results)
    assert len(set(seen)) == 2 and len(set(homes)) == 2
    assert not any(h.exists() for h in homes)
    assert "TOOLFORGE_HOME" not in os.environ


def test_only_selects_and_orders_the_attacks(monkeypatch: pytest.MonkeyPatch, minilocker: None) -> None:
    monkeypatch.setattr(catalog, "ATTACKS", (attack(name="one"), attack(name="two"), attack(name="three")))
    results = run_attacks(["three", "one"], lab_factory=factory([]), deadline_s=10)
    assert [r.id for r in results] == ["three", "one"]


def test_one_failing_attack_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, minilocker: None
) -> None:
    def boom(lab: Lab) -> dict[str, Any]:
        raise RuntimeError("driver bug")

    monkeypatch.setattr(catalog, "ATTACKS", (attack(boom, name="one"), attack(name="two")))
    results = run_attacks(None, lab_factory=factory([]), deadline_s=10)
    assert [r.verdict for r in results] == [INCONCLUSIVE, CONTAINED]


def test_progress_is_reported_before_and_after_each_attack(
    monkeypatch: pytest.MonkeyPatch, minilocker: None
) -> None:
    monkeypatch.setattr(catalog, "ATTACKS", (attack(name="one"),))
    events: list[tuple[str, str | None]] = []
    run_attacks(
        None,
        lab_factory=factory([]),
        deadline_s=10,
        progress=lambda a, r: events.append((a.id, r and r.verdict)),
    )
    assert events == [("one", None), ("one", CONTAINED)]


def test_logging_is_restored_after_a_run(monkeypatch: pytest.MonkeyPatch, minilocker: None) -> None:
    monkeypatch.setattr(catalog, "ATTACKS", (attack(name="one"),))
    run_attacks(None, lab_factory=factory([]), deadline_s=10)
    assert logging.root.manager.disable == logging.NOTSET


def test_the_default_lab_gets_throwaway_settings_with_no_model_configured(
    tmp_path: Path, minilocker: None
) -> None:
    made = Lab(tmp_path, docker_ok=False)
    try:
        assert made.settings.home == tmp_path
        assert not made.settings.llm_base_url and not made.settings.effective_challenger_base_url
    finally:
        made.close()


# --- The report ------------------------------------------------------------------------------------------------------------------


def results() -> list[AttackResult]:
    return [
        AttackResult("alpha", "A", CONTAINED, "3 checks held", [], 1.2),
        AttackResult("beta", "B", INCONCLUSIVE, "unproven: it never got past G3 " + "x" * 300, [], 0.5),
        AttackResult("gamma", "C", SKIPPED, "needs Docker", [], 0.0),
    ]


def test_the_table_has_a_row_per_attack_a_summary_and_the_footer() -> None:
    text = runner.render_table(results())
    lines = text.splitlines()
    assert lines[0].split() == ["id", "verdict", "evidence"]
    assert lines[1].startswith("alpha") and "contained" in lines[1]
    assert all(len(line) <= 112 for line in lines[:4]) and "..." in lines[2]
    assert "contained 1/3, breached 0, inconclusive 1, skipped 1" in text
    assert text.endswith(runner.FOOTER) and "not about all possible attacks" in runner.FOOTER


def test_exit_codes() -> None:
    contained = [AttackResult("a", "A", CONTAINED, "", [], 0)]
    assert runner.exit_code(contained) == 0
    assert runner.exit_code(results()) == 2
    assert runner.exit_code(contained + [AttackResult("b", "B", BREACHED, "", [], 0)]) == 1
    assert runner.exit_code(results() + [AttackResult("b", "B", BREACHED, "", [], 0)]) == 1


def test_the_json_report_is_complete_and_machine_readable(tmp_path: Path) -> None:
    path = runner.write_json(results(), tmp_path / "out", docker="needs Docker: no daemon")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert path.name.startswith("attacks-") and path.suffix == ".json"
    assert document["summary"] == {"contained": 1, "breached": 0, "inconclusive": 1, "skipped": 1}
    assert document["docker"] == "needs Docker: no daemon" and document["footer"] == runner.FOOTER
    assert [r["id"] for r in document["results"]] == ["alpha", "beta", "gamma"]
    assert document["results"][0]["seconds"] == 1.2
