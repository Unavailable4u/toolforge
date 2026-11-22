"""`operator_actions.decide`: what approving and denying do, what they refuse, and the live smoke run."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from verify_helpers import MemoryLedgers, scripted_gates, valid_package

from toolforge.config import Settings
from toolforge.errors import IllegalTransition, RegistryError, SandboxUnavailable
from toolforge.models.results import CallError, CallResult
from toolforge.registry import Registry, lifecycle
from toolforge.services import operator_actions
from toolforge.services.forge import Forge

NETWORK = {"network": ["pypi.org"], "secrets": [], "inputs": False, "browser": False}
SECRET = {"network": [], "secrets": ["github"], "inputs": False, "browser": False}
LIVE = [
    {"input": {"text": "  hello   world "}, "output": {"text": "hello world", "words": 2}},
    {"input": {"text": "live"}, "live": True},
]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def submit(settings, registry, permissions=NETWORK, *, version="0.1.0", **kw):
    forge = Forge(settings, registry, None, gates=scripted_gates())
    forge._pipeline._approval._brokers = lambda: {"github": "api.github.com"}
    report = forge.submit_package(valid_package(version=version, permissions=permissions, **kw), "agent")
    assert report.status == lifecycle.PENDING_APPROVAL, report.text()
    return report.approval["id"]


class SmokeRunner:
    """Stands in for `ToolRunner`: records what it was asked and answers as told."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok, self.calls = ok, []

    def run(self, record, args, **kwargs):
        self.calls.append((record.label, args, kwargs))
        error = None if self.ok else CallError(type="Crash", message="boom", hint="")
        return CallResult(ok=self.ok, result={"x": 1} if self.ok else None, error=error)


def test_approving_makes_the_version_active_and_the_ledger_hears_it(settings, registry) -> None:
    approval_id = submit(settings, registry)
    ledgers = MemoryLedgers()
    done = operator_actions.decide(
        settings, registry, approval_id, "approve", "read the diff", ledger_factory=ledgers
    )
    assert done.outcome == "approved" and done.to_status == lifecycle.ACTIVE
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.ACTIVE
    approval = registry.get_approval(approval_id)
    assert approval.status == "approved" and approval.decided_by == "operator"
    (_, actor, kind, payload) = ledgers.events[0]
    assert actor == "operator" and kind == "forge.approval.granted"
    assert payload["reason"] == "read the diff" and payload["approval_id"] == approval_id
    assert ledgers.paths[0].endswith("ledgers/my_tool@0.1.0/operator.jsonl")
    assert "approved" in done.text()


def test_approving_a_newer_version_deprecates_the_old_one(settings, registry) -> None:
    first = submit(settings, registry)
    operator_actions.decide(settings, registry, first, "approve", "first")
    second = submit(settings, registry, version="0.2.0", parent_version="0.1.0")
    done = operator_actions.decide(settings, registry, second, "approve", "second")
    assert done.replaced == ("my_tool@0.1.0",)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.DEPRECATED
    assert registry.active_version("my_tool").version == "0.2.0"


def test_denying_rejects_the_version(settings, registry) -> None:
    approval_id = submit(settings, registry)
    ledgers = MemoryLedgers()
    done = operator_actions.decide(
        settings, registry, approval_id, "deny", "too broad", ledger_factory=ledgers
    )
    assert done.outcome == "denied" and registry.find_version("my_tool", "0.1.0").status == lifecycle.REJECTED
    assert registry.get_approval(approval_id).status == "denied"
    assert ledgers.types == ["forge.approval.denied"] and ledgers.events[0][3]["reason"] == "too broad"


@pytest.mark.parametrize("reason", ["", "  ", None])
@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_a_reason_is_required(settings, registry, decision, reason) -> None:
    approval_id = submit(settings, registry)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, approval_id, decision, reason)
    assert caught.value.code == "reason_required"
    assert registry.get_approval(approval_id).status == "pending"


def test_a_decision_is_approve_or_deny(settings, registry) -> None:
    approval_id = submit(settings, registry)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, approval_id, "maybe", "x")
    assert caught.value.code == "bad_decision"


def test_an_unknown_approval_is_refused(settings, registry) -> None:
    with pytest.raises(RegistryError) as caught:
        operator_actions.decide(settings, registry, 99, "approve", "x")
    assert caught.value.code == "unknown_approval"


def test_a_decision_cannot_be_changed(settings, registry) -> None:
    approval_id = submit(settings, registry)
    operator_actions.decide(settings, registry, approval_id, "deny", "no")
    for decision in ("approve", "deny"):
        with pytest.raises(IllegalTransition) as caught:
            operator_actions.decide(settings, registry, approval_id, decision, "again")
        assert caught.value.code == "approval_decided"
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.REJECTED


def test_approval_is_bound_to_the_content(settings, registry) -> None:
    approval_id = submit(settings, registry)
    with registry.db.transaction() as conn:  # a request recorded for other content than the version now has
        conn.execute("UPDATE versions SET code_sha = ? WHERE id = 1", ("f" * 64,))
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, approval_id, "approve", "ok")
    assert caught.value.code == "code_changed"
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.PENDING_APPROVAL


def test_a_version_that_is_no_longer_pending_cannot_be_decided(settings, registry) -> None:
    approval_id = submit(settings, registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE versions SET status = 'quarantined' WHERE id = 1")
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, approval_id, "approve", "ok")
    assert caught.value.code == "not_pending"


def test_a_version_that_was_never_built_cannot_be_approved(settings, registry) -> None:
    forge = Forge(settings, registry, None, gates=scripted_gates(blob=False))
    forge._pipeline._approval._brokers = lambda: {}
    forge.submit_package(valid_package(permissions=NETWORK), "agent")
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, 1, "approve", "ok")
    assert caught.value.code == "not_built"


def test_a_brokered_secret_needs_the_service_where_the_operator_approves(settings, registry) -> None:
    approval_id = submit(settings, registry, SECRET)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, approval_id, "approve", "ok", brokers=lambda: {})
    assert caught.value.code == "broker_not_configured" and "MINILOCKER_BROKER" in str(caught.value)
    assert registry.get_approval(approval_id).status == "pending"  # fixable, so nothing was decided
    done = operator_actions.decide(
        settings, registry, approval_id, "approve", "ok", brokers=lambda: {"github": "api.github.com"}
    )
    assert done.outcome == "approved"


def test_live_examples_run_once_before_the_version_becomes_active(settings, registry) -> None:
    approval_id = submit(settings, registry, examples=LIVE)
    smoke = SmokeRunner()
    done = operator_actions.decide(
        settings, registry, approval_id, "approve", "ok", tool_runner_factory=lambda: smoke
    )
    assert done.outcome == "approved" and done.smoke == ("#1: ok",)
    ((label, args, kwargs),) = smoke.calls
    assert label == "my_tool@0.1.0" and args == {"text": "live"}
    assert kwargs["allow_status"] == ("pending_approval",) and kwargs["record_stats"] is False


def test_a_failing_live_example_rejects_the_version(settings, registry) -> None:
    approval_id = submit(settings, registry, examples=LIVE)
    ledgers = MemoryLedgers()
    done = operator_actions.decide(
        settings,
        registry,
        approval_id,
        "approve",
        "ok",
        tool_runner_factory=lambda: SmokeRunner(ok=False),
        ledger_factory=ledgers,
    )
    assert done.outcome == "rejected_by_smoke" and "failed (Crash)" in done.smoke[0]
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.REJECTED
    assert registry.get_approval(approval_id).status == "denied"
    assert ledgers.types == ["forge.approval.denied"] and ledgers.events[0][3]["live_smoke"]


def test_live_examples_without_a_sandbox_change_nothing(settings, registry) -> None:
    approval_id = submit(settings, registry, examples=LIVE)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.decide(settings, registry, approval_id, "approve", "ok")
    assert caught.value.code == "smoke_needs_sandbox"
    assert registry.get_approval(approval_id).status == "pending"


def test_a_sandbox_that_cannot_start_changes_nothing(settings, registry) -> None:
    approval_id = submit(settings, registry, examples=LIVE)

    def broken() -> SimpleNamespace:
        raise SandboxUnavailable("no docker")

    with pytest.raises(SandboxUnavailable):
        operator_actions.decide(settings, registry, approval_id, "approve", "ok", tool_runner_factory=broken)
    assert registry.get_approval(approval_id).status == "pending"
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.PENDING_APPROVAL


def test_no_live_example_means_no_sandbox_is_needed(settings, registry) -> None:
    approval_id = submit(settings, registry)

    def never() -> None:
        raise AssertionError("a sandbox was asked for")

    done = operator_actions.decide(
        settings, registry, approval_id, "approve", "ok", tool_runner_factory=never
    )
    assert done.outcome == "approved"
