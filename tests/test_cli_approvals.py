"""`toolforge approvals list|show|approve|deny`."""

from __future__ import annotations

from pathlib import Path

import pytest
from verify_helpers import MemoryLedgers, scripted_gates, valid_package

from toolforge import cli_approvals, cli_run
from toolforge.cli import main
from toolforge.config import Settings
from toolforge.registry import Registry, lifecycle
from toolforge.services.forge import Forge

NETWORK = {"network": ["pypi.org"], "secrets": [], "inputs": False, "browser": False}


@pytest.fixture
def ledgers(monkeypatch: pytest.MonkeyPatch) -> MemoryLedgers:
    memory = MemoryLedgers()
    monkeypatch.setattr(cli_run, "make_ledger", memory)
    return memory


@pytest.fixture
def pending(tmp_home: Path) -> int:
    """One pending approval request in the home the CLI opens."""
    with Registry(tmp_home) as registry:
        forge = Forge(Settings(home=tmp_home), registry, None, gates=scripted_gates())
        report = forge.submit_package(valid_package(permissions=NETWORK), "agent")
    assert report.approval is not None
    return report.approval["id"]


def out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


def status_of(tmp_home: Path) -> str:
    with Registry(tmp_home) as registry:
        return registry.find_version("my_tool", "0.1.0").status


def test_list_shows_a_pending_request_made_by_a_fixture(pending, ledgers, capsys) -> None:
    assert main(["approvals", "list"]) == 0
    row = out(capsys).strip().split("\t")
    assert row[:3] == [str(pending), "pending", "my_tool@0.1.0"]
    assert row[3] == "network: pypi.org" and row[4] == "secrets: -" and "[G5,G6 not run]" in row[5]


def test_list_says_when_there_is_nothing(tmp_home, ledgers, capsys) -> None:
    assert main(["approvals", "list"]) == 0
    assert "no pending approval requests" in out(capsys)
    assert main(["approvals", "list", "--status", "all"]) == 0
    assert "no approval requests" in out(capsys)


def test_show_prints_the_whole_request(pending, ledgers, capsys) -> None:
    assert main(["approvals", "show", str(pending)]) == 0
    text = out(capsys)
    assert "WEAKER EVIDENCE: G5, G6" in text and "permission diff" in text and "Blast Radius:" in text
    assert "limit: The egress proxy has one allowlist" in text


def test_show_an_unknown_id_is_an_error(tmp_home, ledgers, capsys) -> None:
    assert main(["approvals", "show", "42"]) == 1
    assert "unknown_approval" in capsys.readouterr().err


def test_approve_needs_a_reason(pending, ledgers, capsys) -> None:
    with pytest.raises(SystemExit):
        main(["approvals", "approve", str(pending)])


def test_approve_prints_the_request_first_then_activates(pending, ledgers, tmp_home, capsys) -> None:
    assert main(["approvals", "approve", str(pending), "--reason", "read it"]) == 0
    text = out(capsys)
    assert text.index("requested network hosts") < text.index("is approved and now active")
    assert status_of(tmp_home) == lifecycle.ACTIVE
    assert ledgers.types == ["forge.approval.granted"]


def test_deny_rejects(pending, ledgers, tmp_home, capsys) -> None:
    assert main(["approvals", "deny", str(pending), "--reason", "no"]) == 0
    assert "denied and is rejected" in out(capsys)
    assert status_of(tmp_home) == lifecycle.REJECTED


def test_a_second_decision_is_an_error(pending, ledgers, capsys) -> None:
    main(["approvals", "deny", str(pending), "--reason", "no"])
    capsys.readouterr()
    assert main(["approvals", "approve", str(pending), "--reason", "yes"]) == 1
    assert "approval_decided" in capsys.readouterr().err


def test_list_denies_overdue_requests(pending, ledgers, tmp_home, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOOLFORGE_APPROVAL_TIMEOUT_S", "1")
    with Registry(tmp_home) as registry, registry.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET requested_json = json_set(requested_json, '$.expires_at', '2020-01-01T00:00:00.000Z')"
        )
    assert main(["approvals", "list"]) == 0
    text = out(capsys)
    assert "timed out and was denied" in text and "no pending approval requests" in text
    assert status_of(tmp_home) == lifecycle.REJECTED


def test_a_live_example_failure_exits_non_zero_and_stops_the_sandbox(
    pending, ledgers, tmp_home, monkeypatch, capsys
) -> None:
    from toolforge.models.results import CallError, CallResult

    with Registry(tmp_home) as registry, registry.db.transaction() as conn:
        manifest = registry.find_version("my_tool", "0.1.0").manifest
        manifest["examples"].append({"input": {"text": "x"}, "files": {}, "output": None, "live": True})
        import json

        conn.execute("UPDATE versions SET manifest_json = ?", (json.dumps(manifest, sort_keys=True),))

    class Sandbox:
        stopped = 0

        def shutdown(self) -> None:
            Sandbox.stopped += 1

    class Failing:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, record, args, **kwargs):
            return CallResult(ok=False, error=CallError(type="Crash", message="boom", hint=""))

    monkeypatch.setattr(cli_run, "make_sandbox_runner", lambda settings: Sandbox())
    monkeypatch.setattr(cli_approvals, "ToolRunner", Failing)
    assert main(["approvals", "approve", str(pending), "--reason", "try"]) == 1
    assert "a live example failed" in out(capsys) and Sandbox.stopped == 1
    assert status_of(tmp_home) == lifecycle.REJECTED
