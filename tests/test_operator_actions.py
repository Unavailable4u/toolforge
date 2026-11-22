"""`promote` and `toolforge promote`: what an operator may do, what is refused, and what the ledger says."""

from __future__ import annotations

from pathlib import Path

import pytest
from verify_helpers import MemoryLedgers, scripted_gates, valid_package

from toolforge.cli import main
from toolforge.config import Settings
from toolforge.errors import IllegalTransition, LedgerError, RegistryError
from toolforge.registry import Registry, lifecycle
from toolforge.services import operator_actions
from toolforge.services.forge import Forge


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def submit(settings, registry, *, blob: bool = True, **kwargs) -> str:
    forge = Forge(settings, registry, None, gates=scripted_gates(blob=blob))
    report = forge.submit_package(valid_package(**kwargs), "agent")
    assert report.registered, report.text()
    return f"{report.name}@{report.version}"


def test_promote_makes_a_verified_basic_version_active_and_writes_the_ledger(settings, registry) -> None:
    ref = submit(settings, registry)
    ledgers = MemoryLedgers()
    done = operator_actions.promote(settings, registry, ref, "reviewed by hand", ledger_factory=ledgers)
    assert done.from_status == lifecycle.VERIFIED_BASIC and done.label == ref
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.ACTIVE
    assert ledgers.types == ["forge.promoted"]
    task, actor, _, payload = ledgers.events[0]
    assert actor == "operator" and payload["reason"] == "reviewed by hand" and "type" not in payload
    assert ledgers.paths[0].endswith(f"ledgers/{ref}/operator.jsonl")


def test_a_new_version_deprecates_the_one_it_replaces_and_both_ledgers_hear_of_it(settings, registry) -> None:
    first = submit(settings, registry)
    operator_actions.promote(settings, registry, first, "first")
    second = submit(settings, registry, version="0.2.0", parent_version="0.1.0")
    ledgers = MemoryLedgers()
    done = operator_actions.promote(settings, registry, second, "better", ledger_factory=ledgers)
    assert done.replaced == (first,)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.DEPRECATED
    assert registry.active_version("my_tool").version == "0.2.0"
    assert sorted(ledgers.types) == ["forge.deprecated", "forge.promoted"]
    assert any(p.endswith(f"{first}/operator.jsonl") for p in ledgers.paths)


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_a_reason_is_required(settings, registry, reason) -> None:
    ref = submit(settings, registry)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, ref, reason)
    assert caught.value.code == "reason_required"
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.VERIFIED_BASIC


@pytest.mark.parametrize("ref", ["my_tool", "my_tool@", "@1", "a@b@c", "", None, 5])
def test_a_reference_must_be_name_at_version(settings, registry, ref) -> None:
    with pytest.raises(RegistryError) as caught:
        operator_actions.promote(settings, registry, ref, "why")
    assert caught.value.code == "bad_version_ref"


def test_an_unknown_version_is_refused(settings, registry) -> None:
    with pytest.raises(RegistryError) as caught:
        operator_actions.promote(settings, registry, "ghost@1.0.0", "why")
    assert caught.value.code == "unknown_version"


def force_status(registry: Registry, status: str, ref: str = "my_tool@0.1.0") -> None:
    name, version = ref.split("@")
    record = registry.find_version(name, version)
    with registry.db.transaction() as conn:  # a state that the pipeline reaches by other routes
        conn.execute("UPDATE versions SET status = ? WHERE id = ?", (status, record.id))


@pytest.mark.parametrize(
    ("status", "mentions"),
    [
        (lifecycle.ACTIVE, "already active"),
        (lifecycle.REJECTED, "rejected"),
        (lifecycle.QUARANTINED, "release"),
        (lifecycle.DEPRECATED, "rollback"),
        (lifecycle.PENDING_APPROVAL, "approval"),
        (lifecycle.SUBMITTED, "not been verified"),
        (lifecycle.VERIFYING, "still being verified"),
    ],
)
def test_every_other_status_is_refused_with_the_right_next_step(settings, registry, status, mentions) -> None:
    ref = submit(settings, registry)
    force_status(registry, status)
    ledgers = MemoryLedgers()
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, ref, "why", ledger_factory=ledgers)
    assert caught.value.code == "not_promotable" and mentions in str(caught.value)
    assert registry.find_version("my_tool", "0.1.0").status == status and ledgers.events == []


def test_a_version_without_a_stored_package_is_not_promoted(settings, registry) -> None:
    ref = submit(settings, registry, blob=False)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, ref, "why")
    assert caught.value.code == "not_built" and "frozen package" in str(caught.value)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.VERIFIED_BASIC


def test_a_version_whose_build_gate_did_not_pass_is_not_promoted(settings, registry) -> None:
    ref = submit(settings, registry)
    record = registry.find_version("my_tool", "0.1.0")
    with registry.db.transaction() as conn:
        conn.execute(
            "UPDATE gate_results SET status = 'failed' WHERE version_id = ? AND gate = 'G2'", (record.id,)
        )
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, ref, "why")
    assert caught.value.code == "not_built" and "G2" in str(caught.value)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.VERIFIED_BASIC


def test_a_blob_that_vanished_from_the_store_is_not_promoted(settings, registry) -> None:
    ref = submit(settings, registry)
    record = registry.find_version("my_tool", "0.1.0")
    registry.blobs._path(record.blob_sha).unlink()
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, ref, "why")
    assert caught.value.code == "not_built"


def test_a_version_that_asks_for_network_or_secrets_is_never_promoted(settings, registry) -> None:
    ref = submit(
        settings,
        registry,
        permissions={"network": ["api.example.com"], "secrets": [], "inputs": False},
    )
    force_status(registry, lifecycle.VERIFIED)
    ledgers = MemoryLedgers()
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, ref, "why", ledger_factory=ledgers)
    assert caught.value.code == "needs_approval" and "toolforge approvals" in str(caught.value)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.VERIFIED and ledgers.events == []


def test_a_ledger_failure_after_the_change_is_reported_honestly(settings, registry) -> None:
    ref = submit(settings, registry)

    def broken(path, task_id):
        raise OSError("disk full: /secret/path")

    with pytest.raises(LedgerError) as caught:
        operator_actions.promote(settings, registry, ref, "why", ledger_factory=broken)
    assert "now active" in str(caught.value) and "/secret/path" not in str(caught.value)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.ACTIVE


# --- The command ------------------------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("minilocker")
def test_the_command_promotes_and_prints(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(home=tmp_home)
    with Registry(tmp_home) as registry:
        ref = submit(settings, registry)
    assert main(["promote", ref, "--reason", "from the test"]) == 0
    assert f"{ref} is now active" in capsys.readouterr().out
    with Registry(tmp_home) as registry:
        assert registry.active_version("my_tool") is not None
    ledger = tmp_home / "ledgers" / ref / "operator.jsonl"
    assert ledger.exists() and "forge.promoted" in ledger.read_text()


def test_the_command_needs_a_reason_and_explains_refusals(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["promote", "x@1"])
    capsys.readouterr()
    assert main(["promote", "ghost@1.0.0", "--reason", "why"]) != 0
    assert "ghost@1.0.0" in capsys.readouterr().err


# --- quarantine and release (P08) -------------------------------------------------------------------------------------------------


def active(settings, registry, **kwargs) -> str:
    ref = submit(settings, registry, **kwargs)
    operator_actions.promote(settings, registry, ref, "setup")
    return ref


def quarantined_by_forge(settings, registry, **kwargs) -> str:
    ref = active(settings, registry, **kwargs)
    name, version = operator_actions.parse_label(ref)
    registry.transition(
        registry.find_version(name, version).id, "quarantined", actor="forge", reason="touched a decoy"
    )
    return ref


def test_quarantine_takes_an_active_version_out_of_use_and_writes_the_ledger(settings, registry) -> None:
    ref = active(settings, registry)
    ledgers = MemoryLedgers()
    done = operator_actions.quarantine(settings, registry, ref, "looks odd", ledger_factory=ledgers)
    assert done.action == "quarantined" and done.from_status == lifecycle.ACTIVE
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.QUARANTINED
    assert registry.active_version("my_tool") is None
    assert ledgers.types == ["forge.quarantined"]
    _, actor, _, payload = ledgers.events[0]
    assert actor == "operator" and payload["reason"] == "looks odd"
    assert ledgers.paths[0].endswith(f"ledgers/{ref}/operator.jsonl")


def test_release_makes_a_quarantined_version_active_again(settings, registry) -> None:
    ref = quarantined_by_forge(settings, registry)
    ledgers = MemoryLedgers()
    done = operator_actions.release(settings, registry, ref, "reviewed the report", ledger_factory=ledgers)
    assert done.action == "released" and done.from_status == lifecycle.QUARANTINED
    assert registry.active_version("my_tool").version == "0.1.0"
    assert ledgers.types == ["forge.released"] and ledgers.events[0][1] == "operator"
    assert "active again" in done.text()


def test_releasing_deprecates_a_newer_active_version_and_says_so(settings, registry) -> None:
    old = quarantined_by_forge(settings, registry)
    new = active(settings, registry, version="0.2.0", parent_version="0.1.0")
    context = operator_actions.release_context(settings, registry, old)
    assert context.active_sibling == new and "deprecates" in context.text()
    ledgers = MemoryLedgers()
    done = operator_actions.release(settings, registry, old, "the old one is right", ledger_factory=ledgers)
    assert done.replaced == (new,)
    assert registry.find_version("my_tool", "0.2.0").status == lifecycle.DEPRECATED
    assert sorted(ledgers.types) == ["forge.deprecated", "forge.released"]


@pytest.mark.parametrize("verb", ["quarantine", "release"])
@pytest.mark.parametrize("reason", ["", "  ", None])
def test_both_need_a_reason(settings, registry, verb, reason) -> None:
    ref = active(settings, registry) if verb == "quarantine" else quarantined_by_forge(settings, registry)
    with pytest.raises(IllegalTransition) as caught:
        getattr(operator_actions, verb)(settings, registry, ref, reason)
    assert caught.value.code == "reason_required"


def test_only_an_active_version_can_be_quarantined(settings, registry) -> None:
    ref = submit(settings, registry)  # verified_basic
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.quarantine(settings, registry, ref, "why")
    assert caught.value.code == "not_quarantinable" and lifecycle.VERIFIED_BASIC in str(caught.value)


def test_only_a_quarantined_version_can_be_released_and_the_refusal_names_the_next_step(
    settings, registry
) -> None:
    ref = submit(settings, registry)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.release(settings, registry, ref, "why")
    assert caught.value.code == "not_releasable" and "promote" in caught.value.hint
    operator_actions.promote(settings, registry, ref, "ok")
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.release(settings, registry, ref, "why")
    assert "active already" in caught.value.hint


def test_an_unknown_version_is_refused_by_both(settings, registry) -> None:
    for verb in (operator_actions.quarantine, operator_actions.release):
        with pytest.raises(RegistryError) as caught:
            verb(settings, registry, "ghost@1.0.0", "why")
        assert caught.value.code == "unknown_version"
    with pytest.raises(RegistryError):
        operator_actions.release_context(settings, registry, "ghost@1.0.0")


def test_a_ledger_failure_after_a_quarantine_is_reported_but_the_change_stands(settings, registry) -> None:
    ref = active(settings, registry)

    def broken(path, task_id):
        raise OSError("disk full: /secret/path")

    with pytest.raises(LedgerError) as caught:
        operator_actions.quarantine(settings, registry, ref, "why", ledger_factory=broken)
    assert "now quarantined" in str(caught.value) and "/secret/path" not in str(caught.value)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.QUARANTINED


@pytest.mark.usefixtures("minilocker")
def test_the_context_shows_the_recorded_reason_and_a_missing_report_as_a_gap(settings, registry) -> None:
    ref = quarantined_by_forge(settings, registry)
    from toolforge.ledger import ToolforgeLedger

    path = settings.home / "ledgers" / ref / "operator.jsonl"
    ToolforgeLedger(path, "q").append(
        "forge",
        "forge.quarantined",
        {"reason": "a decoy \x1b[31mwas touched" + "x" * 900, "call_id": "call0001"},
    )
    context = operator_actions.release_context(settings, registry, ref)
    assert context.status == lifecycle.QUARANTINED
    (reason,) = context.quarantine
    assert "a decoy" in reason and "(by forge)" in reason and "\x1b" not in reason and len(reason) < 400
    assert context.blast_radius["status"] == "not_measured"
    text = context.text()
    assert "quarantine reason:" in text and "Blast Radius: not_measured" in text


def test_the_context_without_a_ledger_says_none_recorded(settings, registry) -> None:
    ref = quarantined_by_forge(settings, registry)
    context = operator_actions.release_context(settings, registry, ref)
    assert context.quarantine == () and "none recorded" in context.text()


def test_the_context_writes_nothing(settings, registry) -> None:
    ref = quarantined_by_forge(settings, registry)
    operator_actions.release_context(settings, registry, ref)
    assert not (settings.home / "ledgers").exists()
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.QUARANTINED


@pytest.mark.usefixtures("minilocker")
def test_the_commands_quarantine_and_release_and_print(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(home=tmp_home)
    with Registry(tmp_home) as registry:
        ref = active(settings, registry)
    assert main(["quarantine", ref, "--reason", "from the test"]) == 0
    assert f"{ref} is now quarantined" in capsys.readouterr().out
    assert main(["release", ref, "--reason", "reviewed"]) == 0
    out = capsys.readouterr().out
    assert (
        "quarantine reason: from the test" in out
        and "Blast Radius: not_measured" in out
        and "active again" in out
    )
    with Registry(tmp_home) as registry:
        assert registry.active_version("my_tool") is not None
    ledger = (tmp_home / "ledgers" / ref / "operator.jsonl").read_text()
    assert "forge.quarantined" in ledger and "forge.released" in ledger


def test_the_commands_need_a_reason_and_explain_refusals(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for verb in ("release", "quarantine"):
        with pytest.raises(SystemExit):
            main([verb, "x@1"])
    capsys.readouterr()
    assert main(["release", "ghost@1.0.0", "--reason", "why"]) != 0
    assert "ghost@1.0.0" in capsys.readouterr().err


@pytest.mark.usefixtures("minilocker")
def test_the_report_command_builds_stores_and_prints(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(home=tmp_home)
    with Registry(tmp_home) as registry:
        ref = submit(settings, registry)
    assert main(["report", ref]) == 0
    out = capsys.readouterr().out
    assert f"Blast Radius: {ref}" in out and "not_measured" in out
    assert (tmp_home / "ledgers" / ref / "blast-radius.json").is_file()
    assert main(["report", ref, "--json", "--no-store"]) == 0
    assert '"verdict"' in capsys.readouterr().out
    assert main(["report", ref, "--html"]) == 0
    assert "<html" in capsys.readouterr().out.lower()


def test_the_report_command_does_not_make_folders_for_unknown_or_hostile_names(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["report", "ghost@1.0.0"]) != 0
    assert "ghost@1.0.0" in capsys.readouterr().err
    assert main(["report", "../../evil@1.0.0"]) != 0
    capsys.readouterr()
    assert not (tmp_home / "ledgers").exists()
    with pytest.raises(SystemExit):
        main(["report", "x@1", "--json", "--html"])
