"""G7, the approval gate: when it applies, what it does to the version, and that it cannot approve anything."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from verify_helpers import scripted_gates, valid_package

from toolforge.config import Settings
from toolforge.registry import Registry, lifecycle
from toolforge.services.forge import Forge
from toolforge.verify import g7_approval
from toolforge.verify.g7_approval import ApprovalGate
from toolforge.verify.pipeline import GATE_CLASS_PATHS, PIPELINE_GATE_IDS

SECRET = {"network": [], "secrets": ["github"], "inputs": False, "browser": False}
NETWORK = {"network": ["pypi.org"], "secrets": [], "inputs": False, "browser": False}
NOTHING = {"network": [], "secrets": [], "inputs": False, "browser": False}


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def run(tmp_path: Path, registry: Registry, permissions: dict):
    forge = Forge(Settings(home=tmp_path / "home"), registry, None, gates=scripted_gates())
    forge._pipeline._approval._brokers = lambda: {"github": "api.github.com"}
    return forge.submit_package(valid_package(permissions=permissions), "agent")


def test_g7_is_built_but_is_not_a_step_the_pipeline_waits_in() -> None:
    assert GATE_CLASS_PATHS["G7"] == "toolforge.verify.g7_approval:ApprovalGate"
    assert "G7" not in PIPELINE_GATE_IDS and ApprovalGate.id == "G7"


@pytest.mark.parametrize(("permissions", "needed"), [(NETWORK, True), (SECRET, True), (NOTHING, False)])
def test_g7_applies_exactly_when_the_manifest_asks_for_network_or_secrets(
    tmp_path: Path, registry: Registry, permissions: dict, needed: bool
) -> None:
    report = run(tmp_path, registry, permissions)
    record = registry.find_version("my_tool", "0.1.0")
    assert ApprovalGate.needed(record) is needed
    assert (report.status == lifecycle.PENDING_APPROVAL) is needed


def test_a_verified_basic_network_tool_is_pending_never_active_and_promote_refuses_it(
    tmp_path: Path, registry: Registry
) -> None:
    from toolforge.errors import IllegalTransition
    from toolforge.services import operator_actions

    run(tmp_path, registry, NETWORK)
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.PENDING_APPROVAL
    with pytest.raises(IllegalTransition):
        operator_actions.promote(Settings(home=tmp_path / "home"), registry, "my_tool@0.1.0", "x")
    assert registry.active_version("my_tool") is None


def test_the_lifecycle_has_no_automatic_route_from_pending_to_active() -> None:
    assert lifecycle.EDGES[("pending_approval", "active")].operator_only
    assert all(
        edge.operator_only
        for (_, target), edge in lifecycle.EDGES.items()
        if target == "active" and _ == "pending_approval"
    )


def test_g7_imports_no_decision_function() -> None:
    tree = ast.parse(Path(g7_approval.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert "decide" not in imported and "operator_actions" not in imported
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "toolforge.services.operator_actions" not in modules
