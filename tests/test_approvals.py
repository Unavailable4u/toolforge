"""Approval requests: the state machine, the permission diff, the request document, timeouts and automatic denials."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from verify_helpers import MemoryLedgers, scripted_gates, valid_package

from toolforge.config import Settings
from toolforge.errors import IllegalTransition
from toolforge.registry import Registry, lifecycle
from toolforge.services import approvals, operator_actions
from toolforge.services.forge import Forge

NETWORK = {"network": ["pypi.org", "api.example.com"], "secrets": [], "inputs": False, "browser": False}
SECRET = {"network": [], "secrets": ["github"], "inputs": False, "browser": False}
BROKERS = {"github": "api.github.com"}


class Clock:
    """A registry clock a test can move: ISO strings in the registry's own format."""

    def __init__(self) -> None:
        self.now = "2026-11-22T10:00:00.000Z"

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home", approval_timeout_s=900)


@pytest.fixture
def registry(tmp_path: Path, clock: Clock):
    with Registry(tmp_path / "home", clock=clock) as reg:
        yield reg


def submit(settings, registry, permissions, *, version="0.1.0", brokers=lambda: BROKERS, ledgers=None, **kw):
    forge = Forge(settings, registry, None, gates=scripted_gates(), ledger_factory=ledgers)
    forge._pipeline._approval._brokers = (
        brokers  # the lookup of brokered services a real run asks MiniLocker for
    )
    return forge.submit_package(valid_package(version=version, permissions=permissions, **kw), "agent")


def test_a_network_tool_goes_to_pending_approval_even_with_g5_and_g6_skipped(settings, registry) -> None:
    report = submit(settings, registry, NETWORK)
    assert report.status == lifecycle.PENDING_APPROVAL and report.ok
    assert report.approval == {"id": 1, "state": "pending"}
    assert [g.id for g in report.gates if g.status == "skipped"] == ["G5", "G6"]
    assert "toolforge approvals show 1" in report.next_action and "G5 and G6" in report.next_action
    record = registry.find_version("my_tool", "0.1.0")
    assert record.status == lifecycle.PENDING_APPROVAL
    assert registry.get_approval(1).status == "pending"


def test_promote_still_refuses_a_pending_version(settings, registry) -> None:
    submit(settings, registry, NETWORK)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, "my_tool@0.1.0", "because")
    assert caught.value.code == "not_promotable" and "approvals" in str(caught.value)


def test_a_tool_that_asks_for_nothing_skips_g7(settings, registry) -> None:
    report = submit(settings, registry, {"network": [], "secrets": [], "inputs": False, "browser": False})
    assert report.status == lifecycle.VERIFIED_BASIC and report.approval is None
    assert registry.list_approvals() == []


def test_a_fully_verified_network_tool_also_needs_approval(settings, registry) -> None:
    forge = Forge(settings, registry, None, gates=[*scripted_gates()])
    # G5 and G6 are skipped by the pipeline when no gate object exists; give them scripted passes to reach `verified`
    from verify_helpers import ScriptedGate

    forge._pipeline._gates["G5"] = ScriptedGate("G5")
    forge._pipeline._gates["G6"] = ScriptedGate("G6")
    report = forge.submit_package(valid_package(permissions=NETWORK), "agent")
    assert report.status == lifecycle.PENDING_APPROVAL
    doc = registry.get_approval(1).requested
    assert doc["skipped_gates"] == []


def test_the_request_names_what_is_asked_and_what_was_skipped(settings, registry) -> None:
    submit(settings, registry, NETWORK)
    doc = registry.get_approval(1).requested
    assert doc["requested"] == {"network": ["api.example.com", "pypi.org"], "secrets": []}
    assert (
        doc["skipped_gates"] == ["G5", "G6"]
        and doc["code_sha"] == registry.find_version("my_tool", "0.1.0").code_sha
    )
    assert doc["permission_diff"]["no_active_version"] is True
    assert doc["permission_diff"]["network"]["added"] == ["api.example.com", "pypi.org"]
    assert doc["limits"] and "one allowlist" in doc["limits"][0]
    assert doc["blast_radius"]["status"] and doc["expires_at"] > doc["created_at"]


def test_the_request_is_also_written_to_the_approvals_folder(settings, registry) -> None:
    submit(settings, registry, NETWORK)
    path = approvals.request_path(settings, 1)
    assert path == settings.home / "approvals" / "1.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["approval_id"] == 1 and written["tool"] == "my_tool@0.1.0"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not list(path.parent.glob(".*.tmp"))


def test_the_ledger_hears_of_the_request(settings, registry) -> None:
    ledgers = MemoryLedgers()
    submit(settings, registry, NETWORK, ledgers=ledgers)
    assert "forge.approval.requested" in ledgers.types
    payload = next(p for _, _, t, p in ledgers.events if t == "forge.approval.requested")
    assert payload["approval_id"] == 1 and payload["requested"]["network"] == ["api.example.com", "pypi.org"]
    assert payload["to"] == "pending_approval" and payload["from"] in ("verified", "verified_basic")


def test_the_permission_diff_shows_additions_kept_and_removed() -> None:
    diff = approvals.permission_diff(
        {"network": ["a.example", "b.example"], "secrets": ["github"]},
        {"network": ["b.example", "c.example"], "secrets": ["slack"]},
        "t@1.0.0",
    )
    assert diff["against"] == "t@1.0.0" and diff["no_active_version"] is False
    assert diff["network"] == {"added": ["a.example"], "kept": ["b.example"], "removed": ["c.example"]}
    assert diff["secrets"] == {"added": ["github"], "kept": [], "removed": ["slack"]}


def test_a_new_version_is_diffed_against_the_active_one(settings, registry) -> None:
    submit(settings, registry, {"network": ["pypi.org"], "secrets": [], "inputs": False, "browser": False})
    operator_actions.decide(settings, registry, 1, "approve", "ok")
    submit(settings, registry, NETWORK, version="0.2.0", parent_version="0.1.0")
    diff = registry.get_approval(2).requested["permission_diff"]
    assert diff["against"] == "my_tool@0.1.0"
    assert diff["network"] == {"added": ["api.example.com"], "kept": ["pypi.org"], "removed": []}


def test_the_description_is_cleaned_and_clipped(settings, registry) -> None:
    from dataclasses import replace

    submit(settings, registry, NETWORK)
    record = registry.find_version("my_tool", "0.1.0")
    hostile = (
        "Counts words. " + "x" * 900 + "\u200b\u202e\x1b[31m"
    )  # the registry only holds valid text; this is what hostile input would be
    record = replace(record, manifest={**record.manifest, "description": hostile})
    doc = approvals.build_request(
        record, active=None, gates=[], blast_radius={}, created_at="a", expires_at="b", brokers={}
    )
    description = doc["description"]
    assert len(description) <= approvals.DESCRIPTION_CHARS
    assert all(ch not in description for ch in ("\u200b", "\u202e", "\x1b"))


def test_no_secret_value_reaches_the_request(settings, registry, monkeypatch) -> None:
    monkeypatch.setenv("MINILOCKER_BROKER", "github=api.github.com")
    monkeypatch.setenv("MINILOCKER_BROKER_TOKEN_GITHUB", "ghp_SENTINELVALUE0123456789")
    submit(settings, registry, SECRET, brokers=lambda: {"github": "api.github.com"})
    text = json.dumps(registry.get_approval(1).requested) + approvals.request_path(settings, 1).read_text()
    assert "github" in text  # the name of the service is there
    assert (
        "SENTINELVALUE" not in text
    )  # the token is in this process's environment and nowhere in what was written


def test_a_secret_without_a_broker_service_is_rejected(settings, registry) -> None:
    ledgers = MemoryLedgers()
    report = submit(settings, registry, SECRET, brokers=lambda: {}, ledgers=ledgers)
    assert report.status == lifecycle.REJECTED and report.approval["state"] == "denied"
    assert "github" in report.next_action and "MINILOCKER_BROKER" in report.next_action
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.REJECTED
    approval = registry.get_approval(1)
    assert approval.status == "denied" and approval.decided_by == "forge"
    assert "forge.approval.requested" in ledgers.types and "forge.approval.denied" in ledgers.types


def test_an_unreadable_broker_configuration_is_a_rejection_not_a_crash(
    settings, registry, monkeypatch
) -> None:
    monkeypatch.setattr(
        approvals, "default_brokers", lambda s=None: ({}, "the broker configuration could not be read: X")
    )
    forge = Forge(settings, registry, None, gates=scripted_gates())
    report = forge.submit_package(valid_package(permissions=SECRET), "agent")
    assert report.status == lifecycle.REJECTED
    assert "could not be read" in registry.get_approval(1).requested["broker_problem"]


def test_an_overdue_request_is_denied(settings, registry, clock) -> None:
    ledgers = MemoryLedgers()
    submit(settings, registry, NETWORK)
    clock.now = "2026-11-22T10:14:59.000Z"
    assert approvals.expire_overdue(settings, registry, ledger_factory=ledgers) == []
    clock.now = "2026-11-22T10:15:00.001Z"
    (gone,) = approvals.expire_overdue(settings, registry, ledger_factory=ledgers)
    assert gone.label == "my_tool@0.1.0"
    assert registry.get_approval(1).status == "denied" and registry.get_approval(1).decided_by == "forge"
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.REJECTED
    (_, actor, kind, payload) = ledgers.events[0]
    assert actor == "forge" and kind == "forge.approval.denied" and payload["timed_out"] is True
    assert approvals.expire_overdue(settings, registry, ledger_factory=ledgers) == []  # once only


def test_an_approval_that_timed_out_cannot_be_granted_later(settings, registry, clock) -> None:
    submit(settings, registry, NETWORK)
    clock.now = "2026-11-22T11:00:00.000Z"
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, 1, "approve", "late but fine")
    assert caught.value.code == "approval_decided"
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.REJECTED


def test_a_request_with_no_readable_expiry_is_left_alone(settings, registry, clock) -> None:
    submit(settings, registry, NETWORK)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE approvals SET requested_json = '{}' WHERE id = 1")
    clock.now = "2027-01-01T00:00:00.000Z"
    assert approvals.expire_overdue(settings, registry) == []
    assert registry.get_approval(1).status == "pending"


def test_only_the_asked_version_is_swept(settings, registry, clock) -> None:
    submit(settings, registry, NETWORK)
    submit(
        settings,
        registry,
        {"network": ["pypi.org"], "secrets": [], "inputs": False, "browser": False},
        version="0.2.0",
    )
    clock.now = "2027-01-01T00:00:00.000Z"
    second = registry.find_version("my_tool", "0.2.0")
    (gone,) = approvals.expire_overdue(settings, registry, version_id=second.id)
    assert gone.label == "my_tool@0.2.0" and registry.get_approval(1).status == "pending"


def test_submitting_again_opens_the_request_of_a_version_that_was_left_verified(settings, registry) -> None:
    submit(settings, registry, NETWORK)
    # an earlier run that failed after the last gate: the version is verified, with no open request
    with registry.db.transaction() as conn:
        conn.execute("UPDATE versions SET status = 'verified_basic'")
        conn.execute("DELETE FROM approvals")
    report = submit(settings, registry, NETWORK)
    assert report.status == lifecycle.PENDING_APPROVAL and registry.list_approvals("pending")


def test_a_version_that_needs_no_approval_cannot_open_a_request(settings, registry) -> None:
    submit(settings, registry, {"network": [], "secrets": [], "inputs": False, "browser": False})
    record = registry.find_version("my_tool", "0.1.0")
    with pytest.raises(IllegalTransition) as caught:
        approvals.open_request(settings, registry, record, [])
    assert caught.value.code == "no_approval_needed"


def test_a_dry_run_says_approval_will_follow(settings, tmp_path) -> None:
    from verify_helpers import ScriptedGate

    from toolforge.verify.pipeline import Pipeline

    pipeline = Pipeline(settings, None, None, gates=[ScriptedGate(g) for g in ("G1", "G2", "G3", "G4")])
    report = pipeline.run(valid_package(permissions=NETWORK), "cli")
    assert report.status == lifecycle.VERIFIED_BASIC and report.dry_run
    assert "approval" in report.next_action and "promote does not apply" in report.next_action


def test_the_text_for_the_operator_leads_with_the_weaker_evidence(settings, registry) -> None:
    submit(settings, registry, NETWORK)
    text = approvals.render_text(registry.get_approval(1), "pending_approval")
    lines = text.splitlines()
    assert lines[0].startswith("approval 1: my_tool@0.1.0 is pending")
    assert lines[1].startswith("WEAKER EVIDENCE: G5, G6")
    assert "requested network hosts: api.example.com, pypi.org" in text and "limit: " in text


def test_a_version_whose_gates_did_not_all_pass_is_never_put_up_for_approval(settings, registry) -> None:
    submit(settings, registry, NETWORK)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE versions SET status = 'verified_basic'")
        conn.execute("DELETE FROM approvals")
        conn.execute("UPDATE gate_results SET status = 'failed' WHERE gate = 'G3'")
    record = registry.find_version("my_tool", "0.1.0")
    with pytest.raises(IllegalTransition) as caught:
        approvals.open_request(settings, registry, record, [])
    assert caught.value.code == "gates_not_passed" and "G3" in str(caught.value)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.VERIFIED_BASIC
