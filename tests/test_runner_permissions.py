"""P09 at run time: the manifest's permissions become the sandbox's network, brokered secrets are placeholders, nothing unapproved runs.

Uses the same scripted sandbox as `test_tool_runner.py`; no Docker. The real proxy is proved in `tests/docker/test_egress.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_tool_runner import Rig, operator_events

from toolforge.errors import ToolNotRunnable
from toolforge.services import runner as runner_module
from toolforge.services.runner import real_secret_audit

NETWORK = {"network": ["api.example.com"], "secrets": [], "inputs": False, "browser": False}
SECRET = {"network": [], "secrets": ["github"], "inputs": False, "browser": False}


@pytest.fixture
def rig(tmp_path: Path, make_payload: Callable[[], dict], monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(runner_module, "ARG_CHECK_ISOLATED", False)
    r = Rig(tmp_path, make_payload)
    r.runner._broker_services = lambda: {"github": "api.github.com"}
    r.succeeds()
    return r


def register(rig: Rig, permissions: dict, *, status: str = "active", approved: bool = True):
    record = rig.register(rig.package(permissions=permissions), status=status)
    if approved:
        rig.registry.approvals.append(approval(record, permissions))
    return record


def approval(record, permissions: dict, *, status: str = "approved", code_sha: str | None = None):
    return SimpleNamespace(
        version_id=record.id,
        status=status,
        requested={
            "code_sha": code_sha or record.code_sha,
            "requested": {
                "network": sorted(permissions["network"]),
                "secrets": sorted(permissions["secrets"]),
            },
        },
    )


def started(rig: Rig) -> dict:
    return next(e for e in rig.events if e["type"] == "forge.call.started")["payload"]


def finished(rig: Rig) -> dict:
    return next(e for e in rig.events if e["type"] == "forge.call.finished")["payload"]


def test_the_manifests_hosts_become_the_sandboxes_network(rig: Rig) -> None:
    register(rig, NETWORK)
    assert rig.run().ok is True
    (spec,) = rig.fake.opened
    assert spec.network == ("api.example.com",) and spec.broker == ()
    assert started(rig)["network"] == ["api.example.com"] and started(rig)["secrets"] == []


def test_a_tool_with_no_hosts_has_no_network(rig: Rig) -> None:
    register(rig, {"network": [], "secrets": [], "inputs": False, "browser": False}, approved=False)
    rig.run()
    (spec,) = rig.fake.opened
    assert spec.network == "none" and spec.broker == ()
    assert started(rig)["network"] == "none"


def test_a_secret_is_a_brokered_service_with_no_network_hosts(rig: Rig) -> None:
    register(rig, SECRET)
    assert rig.run().ok is True
    (spec,) = rig.fake.opened
    assert spec.network == "none" and spec.broker == ("github",)
    assert (
        spec.env == {}
    )  # nothing secret is put in the environment by ToolForge: MiniLocker adds placeholders only
    assert started(rig)["secrets"] == ["github"]


def test_a_secret_without_a_broker_service_is_refused(rig: Rig) -> None:
    rig.runner._broker_services = lambda: {}
    register(rig, SECRET)
    with pytest.raises(ToolNotRunnable) as caught:
        rig.run()
    assert caught.value.code == "broker_not_configured" and "MINILOCKER_BROKER" in str(caught.value)
    assert rig.fake.opened == [] and rig.events == []


def test_a_network_tool_without_a_recorded_approval_is_refused(rig: Rig) -> None:
    register(rig, NETWORK, approved=False)
    with pytest.raises(ToolNotRunnable) as caught:
        rig.run()
    assert caught.value.code == "permissions_not_approved"
    assert rig.fake.opened == []


def test_an_approval_for_other_content_does_not_count(rig: Rig) -> None:
    record = register(rig, NETWORK, approved=False)
    rig.registry.approvals.append(approval(record, NETWORK, code_sha="0" * 64))
    rig.registry.approvals.append(approval(record, {"network": ["other.example.com"], "secrets": []}))
    rig.registry.approvals.append(approval(record, NETWORK, status="denied"))
    with pytest.raises(ToolNotRunnable):
        rig.run()
    assert rig.fake.opened == []


@pytest.mark.parametrize("status", ["verified", "verified_basic", "deprecated"])
def test_a_network_tool_that_is_not_active_does_not_run(rig: Rig, status: str) -> None:
    record = register(rig, NETWORK, status=status)
    with pytest.raises(ToolNotRunnable) as caught:
        rig.runner.run(record, {"text": "a"}, allow_status=(status,))
    assert caught.value.code == "permissions_not_approved" and rig.fake.opened == []


def test_the_live_smoke_run_has_the_network_and_leaves_the_statistics_alone(rig: Rig) -> None:
    record = register(rig, NETWORK, status="pending_approval", approved=False)
    result = rig.runner.run(record, {"text": "a"}, allow_status=("pending_approval",), record_stats=False)
    assert result.ok is True
    assert rig.fake.opened[0].network == ("api.example.com",)
    assert rig.registry.calls == []


def test_an_unverified_run_gets_no_network_and_no_secret(rig: Rig) -> None:
    for permissions in (NETWORK, SECRET):
        with pytest.raises(ToolNotRunnable) as caught:
            rig.runner.run_unverified(rig.package(permissions=permissions), {"text": "a"})
        assert caught.value.code == "unverified_no_permissions"
    assert rig.fake.opened == []


def test_a_real_secret_in_the_sandbox_fails_the_call_and_quarantines(rig: Rig) -> None:
    register(rig, SECRET)
    rig.fake._measured = {
        "secrets": {
            "checked": ["MINILOCKER_BROKER_TOKEN_GITHUB"],
            "found": ["MINILOCKER_BROKER_TOKEN_GITHUB"],
            "inside": True,
        }
    }
    result = rig.run()
    assert result.ok is False and result.error is not None and result.error.type == "PolicyViolation"
    assert result.result is None and result.stdout_tail == "" and result.stderr_tail == ""
    assert rig.registry.records[1].status == "quarantined"
    assert "real secret" in rig.registry.transitions[0][3]
    assert "MINILOCKER_BROKER_TOKEN_GITHUB" in rig.registry.transitions[0][3]  # a name, never a value
    done = finished(rig)
    assert (
        done["real_secret_audit"] == "found"
        and done["real_secrets_found"] == 1
        and done["quarantined"] is True
    )
    assert "real_secrets" not in done
    assert len(operator_events(rig)) == 1


def test_a_clean_audit_is_recorded_as_clean(rig: Rig) -> None:
    register(rig, SECRET)
    rig.fake._measured = {"secrets": {"checked": ["X_TOKEN"], "found": [], "inside": True}}
    assert rig.run().ok is True
    assert finished(rig)["real_secret_audit"] == "clean"


def test_an_audit_that_could_not_look_is_not_a_pass(rig: Rig) -> None:
    register(rig, SECRET)
    assert rig.run().ok is True  # the fake measured nothing
    assert finished(rig)["real_secret_audit"] == "not_measured"


@pytest.mark.parametrize(
    ("measured", "state"),
    [
        (None, "not_measured"),
        ({}, "not_measured"),
        ({"secrets": "garbled"}, "not_measured"),
        ({"secrets": {"checked": [], "found": [], "inside": True}}, "not_measured"),
        ({"secrets": {"checked": ["A_TOKEN"], "found": [], "inside": False}}, "not_measured"),
        ({"secrets": {"checked": ["A_TOKEN"], "found": [], "inside": True}}, "clean"),
        ({"secrets": {"checked": ["A_TOKEN"], "found": ["A_TOKEN"], "inside": True}}, "found"),
        ({"secrets": {"found": ["A_TOKEN"]}}, "found"),
    ],
)
def test_real_secret_audit_reads_the_audit_defensively(measured, state: str) -> None:
    assert real_secret_audit(measured)["state"] == state
