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
    assert caught.value.code == "needs_approval" and "P09" in str(caught.value)
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
