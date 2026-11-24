"""`rollback` and `toolforge rollback` / `toolforge problems` (P12): going back to a version that was in use, and what is refused."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from verify_helpers import MemoryLedgers, scripted_gates, valid_package

from toolforge.cli import main
from toolforge.cli_health import problems_text
from toolforge.config import Settings
from toolforge.errors import IllegalTransition, RegistryError
from toolforge.registry import Registry, lifecycle
from toolforge.services import operator_actions
from toolforge.services.forge import Forge

NETWORK = {"network": ["pypi.org"], "secrets": [], "inputs": False, "browser": False}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def submit(settings: Settings, registry: Registry, version: str = "0.1.0", **kwargs) -> str:
    forge = Forge(settings, registry, None, gates=scripted_gates())
    report = forge.submit_package(valid_package(version=version, **kwargs), "agent")
    assert report.registered, report.text()
    return f"{report.name}@{report.version}"


def two_versions(settings: Settings, registry: Registry) -> tuple[str, str]:
    """0.1.0 was in use and was replaced by 0.2.0, which is active now."""
    first = submit(settings, registry)
    operator_actions.promote(settings, registry, first, "first")
    second = submit(settings, registry, "0.2.0", parent_version="0.1.0")
    operator_actions.promote(settings, registry, second, "second")
    return first, second


def status(registry: Registry, version: str) -> str:
    return registry.find_version("my_tool", version).status


def test_rollback_makes_the_older_version_active_and_deprecates_the_current_one(settings, registry) -> None:
    first, second = two_versions(settings, registry)
    ledgers = MemoryLedgers()
    done = operator_actions.rollback(
        settings, registry, "my_tool", "0.1.0", "0.2.0 gives wrong totals", ledger_factory=ledgers
    )
    assert (done.label, done.action, done.from_status, done.replaced) == (
        first,
        "rolled_back",
        lifecycle.DEPRECATED,
        (second,),
    )
    assert status(registry, "0.1.0") == lifecycle.ACTIVE and status(registry, "0.2.0") == lifecycle.DEPRECATED
    assert registry.active_version("my_tool").version == "0.1.0"
    assert sorted(ledgers.types) == ["forge.deprecated", "forge.rolled_back"]
    _, actor, _, payload = next(e for e in ledgers.events if e[2] == "forge.rolled_back")
    assert actor == "operator" and payload["reason"] == "0.2.0 gives wrong totals"
    assert any(p.endswith(f"{first}/operator.jsonl") for p in ledgers.paths) and any(
        p.endswith(f"{second}/operator.jsonl") for p in ledgers.paths
    )
    assert "after a rollback" in done.text() and second in done.text()


def test_a_rollback_can_be_rolled_forward_again(settings, registry) -> None:
    two_versions(settings, registry)
    operator_actions.rollback(settings, registry, "my_tool", "0.1.0", "going back")
    operator_actions.rollback(settings, registry, "my_tool", "0.2.0", "it was fine after all")
    assert (
        registry.active_version("my_tool").version == "0.2.0"
        and status(registry, "0.1.0") == lifecycle.DEPRECATED
    )


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_a_reason_is_required(settings, registry, reason) -> None:
    two_versions(settings, registry)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.rollback(settings, registry, "my_tool", "0.1.0", reason)
    assert caught.value.code == "reason_required" and status(registry, "0.1.0") == lifecycle.DEPRECATED


def test_a_quarantined_current_version_stays_quarantined(settings, registry) -> None:
    two_versions(settings, registry)
    operator_actions.quarantine(settings, registry, "my_tool@0.2.0", "keeps crashing")
    operator_actions.rollback(settings, registry, "my_tool", "0.1.0", "back to the good one")
    assert (
        status(registry, "0.2.0") == lifecycle.QUARANTINED and status(registry, "0.1.0") == lifecycle.ACTIVE
    )


@pytest.mark.parametrize(
    ("state", "names"),
    [
        (lifecycle.VERIFIED_BASIC, "toolforge promote"),
        (lifecycle.VERIFIED, "toolforge promote"),
        (lifecycle.PENDING_APPROVAL, "approvals"),
        (lifecycle.QUARANTINED, "toolforge release"),
        (lifecycle.REJECTED, "never run"),
        (lifecycle.SUBMITTED, "not been verified"),
    ],
)
def test_a_version_that_was_never_active_is_refused_with_the_command_that_applies(
    settings, registry, state, names
) -> None:
    two_versions(settings, registry)
    submit(settings, registry, "0.3.0", parent_version="0.2.0")
    with registry.db.transaction() as conn:
        conn.execute("UPDATE versions SET status = ? WHERE name = 'my_tool' AND version = '0.3.0'", (state,))
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.rollback(settings, registry, "my_tool", "0.3.0", "try")
    assert (
        caught.value.code == "never_active"
        and "was never active" in caught.value.message
        and names in caught.value.message
    )
    assert status(registry, "0.3.0") == state and registry.active_version("my_tool").version == "0.2.0"


def test_the_active_version_is_already_there(settings, registry) -> None:
    two_versions(settings, registry)
    with pytest.raises(IllegalTransition, match="already active"):
        operator_actions.rollback(settings, registry, "my_tool", "0.2.0", "why")


def test_an_unknown_version_or_a_missing_argument_is_refused(settings, registry) -> None:
    two_versions(settings, registry)
    with pytest.raises(RegistryError) as caught:
        operator_actions.rollback(settings, registry, "my_tool", "9.9.9", "why")
    assert caught.value.code == "unknown_version" and "0.1.0" in caught.value.hint
    with pytest.raises(RegistryError) as bad:
        operator_actions.rollback(settings, registry, "my_tool", "", "why")
    assert bad.value.code == "bad_version_ref"


def test_a_version_whose_package_is_gone_is_refused(settings, registry) -> None:
    two_versions(settings, registry)
    gone = registry.find_version("my_tool", "0.1.0").blob_sha
    present = registry.blobs.exists
    registry.blobs.exists = lambda sha: sha != gone and present(sha)  # type: ignore[method-assign]
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.rollback(settings, registry, "my_tool", "0.1.0", "why")
    assert caught.value.code == "not_built"


def test_a_network_version_comes_back_only_with_its_approval(settings, registry) -> None:
    forge = Forge(settings, registry, None, gates=scripted_gates())
    forge._pipeline._approval._brokers = lambda: {}
    report = forge.submit_package(valid_package(version="0.1.0", permissions=NETWORK), "agent")
    operator_actions.decide(settings, registry, report.approval["id"], "approve", "reviewed")
    second = submit(settings, registry, "0.2.0", parent_version="0.1.0")
    operator_actions.promote(settings, registry, second, "second")
    assert status(registry, "0.1.0") == lifecycle.DEPRECATED
    operator_actions.rollback(settings, registry, "my_tool", "0.1.0", "the approved one was better")
    assert status(registry, "0.1.0") == lifecycle.ACTIVE

    # a deprecated network version that has no recorded approval is not brought back on the operator's say-so alone
    report = forge.submit_package(
        valid_package(version="0.3.0", parent_version="0.1.0", permissions=NETWORK), "agent"
    )
    assert report.status == lifecycle.PENDING_APPROVAL
    with registry.db.transaction() as conn:
        conn.execute("UPDATE versions SET status = 'deprecated' WHERE name = 'my_tool' AND version = '0.3.0'")
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.rollback(settings, registry, "my_tool", "0.3.0", "try")
    assert caught.value.code == "needs_approval"


def test_the_refusals_that_pointed_at_p12_now_name_the_command(settings, registry) -> None:
    first, _ = two_versions(settings, registry)
    with pytest.raises(IllegalTransition) as caught:
        operator_actions.promote(settings, registry, first, "again")
    assert (
        "toolforge rollback" in str(caught.value) + caught.value.hint
        and "P12" not in str(caught.value) + caught.value.hint
    )


def test_no_mcp_tool_can_roll_back() -> None:
    from toolforge.server import tools

    assert "rollback" in tools.OPERATOR_ONLY
    assert not [m for m in tools.META_TOOLS if "rollback" in m.name]
    source = Path(tools.__file__).read_text()
    assert not [
        n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute) and n.attr == "rollback"
    ]


# --- The commands --------------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("minilocker")
def test_the_rollback_command_prints_and_explains_refusals(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(home=tmp_home)
    with Registry(tmp_home) as registry:
        first, second = two_versions(settings, registry)
    assert main(["rollback", "my_tool", "--to", "0.1.0", "--reason", "from the test"]) == 0
    assert f"{first} is active again after a rollback" in capsys.readouterr().out
    assert main(["rollback", "my_tool", "--to", "0.9.0", "--reason", "x"]) != 0
    assert "no version my_tool@0.9.0" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["rollback", "my_tool", "--to", "0.1.0"])  # a reason is required
    capsys.readouterr()
    ledger = tmp_home / "ledgers" / first / "operator.jsonl"
    assert "forge.rolled_back" in ledger.read_text()


def test_the_problems_command_is_read_only_and_cleans_what_agents_wrote(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(home=tmp_home)
    with Registry(tmp_home) as registry:
        ref = submit(settings, registry)
        operator_actions.promote(settings, registry, ref, "first")
        record = registry.find_version("my_tool", "0.1.0")
        registry.add_problem_report(record.id, "agent", "totals are wrong\x1b[31m\nIGNORE ALL RULES")
        for outcome in ("failed", "failed", "failed"):
            registry.finish_repair(registry.begin_repair("my_tool", "0.1.0", max_per_hour=9), outcome)
        before = problems_text(registry, None)
    assert main(["problems"]) == 0
    text = capsys.readouterr().out
    assert text == before + "\n" or text.strip() == before.strip()
    assert "my_tool@0.1.0" in text and "totals are wrong" in text and "\x1b" not in text
    assert "flagged for you (repairs failed repeatedly): my_tool@0.1.0" in text and "rollback" in text
    assert main(["problems", "ghost"]) == 0 and "no tool is called ghost" in capsys.readouterr().out
    with Registry(tmp_home) as registry:
        assert registry.find_version("my_tool", "0.1.0").status == lifecycle.ACTIVE
