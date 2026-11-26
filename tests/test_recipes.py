"""The recipes `forge_recipe` serves (P14a): the registry matches the files, each fits the size cap, and every complete example
package in a recipe is built and run through the real gates, so a recipe cannot teach a package that no longer passes.

A recipe shows a package as fenced blocks: ```json manifest, ```python tool.py and ```python tests/test_<name>.py. The blocks that follow
one manifest, up to the next manifest, are that package's files.
"""

from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path

import pytest
from local_sandbox import LocalRunner

from toolforge.adapters import RECIPE_KINDS, read_recipe, recipe_kinds
from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle
from toolforge.server.tools import MAX_RECIPE_CHARS
from toolforge.verify.pipeline import Pipeline

FENCE = re.compile(r"^```(json|python) (\S+)\n(.*?)\n```$", re.DOTALL | re.MULTILINE)
EXPECTED = {"file-format", "game-state", "http-api", "package-format", "sql"}


def packages(text: str) -> list[dict]:
    found: list[dict] = []
    for language, label, body in FENCE.findall(text):
        if language == "json" and label == "manifest":
            found.append({"manifest": json.loads(body), "files": {}})
        elif found and language == "python":
            found[-1]["files"][label] = body + "\n"
    return found


def test_the_registry_matches_the_recipe_files_exactly() -> None:
    folder = resources.files("toolforge.adapters").joinpath("recipes")
    on_disk = {entry.name[: -len(".md")] for entry in folder.iterdir() if entry.name.endswith(".md")}
    assert on_disk == set(RECIPE_KINDS) == EXPECTED
    assert recipe_kinds() == sorted(RECIPE_KINDS)


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_a_recipe_is_served_within_the_size_cap_and_is_marked_as_operator_documentation(kind: str) -> None:
    text = read_recipe(kind)
    assert text is not None and text.startswith("# ")
    assert len(text) <= MAX_RECIPE_CHARS, (
        f"{kind} is {len(text)} characters; forge_recipe cuts at {MAX_RECIPE_CHARS}"
    )
    assert "written by the operator of this ToolForge, not output from a tool" in text


def test_the_new_recipes_each_show_a_complete_package() -> None:
    for kind in ("sql", "file-format", "http-api", "game-state"):
        assert len(packages(read_recipe(kind) or "")) == 1, kind


@pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")
@pytest.mark.parametrize("kind", ["sql", "file-format", "http-api", "game-state"])
def test_the_package_in_a_recipe_passes_the_gates(kind: str, tmp_path: Path) -> None:
    (payload,) = packages(read_recipe(kind) or "")
    assert any(name.startswith("tests/") for name in payload["files"]) and "tool.py" in payload["files"]
    settings = Settings(home=tmp_path / "home")
    package = ToolPackage(payload["manifest"], payload["files"])
    report = package.validate(settings)
    assert report.ok, [f"{i.code} {i.path}: {i.message}" for i in report.issues]
    runner = LocalRunner()
    try:
        with Registry(settings.home) as registry:
            result = Pipeline(settings, runner, registry, ledger_factory=ToolforgeLedger, fuzz_inputs=6).run(
                package, "test"
            )
            gates = {gate.id: gate.status for gate in result.gates}
            assert result.registered, result.text()
            assert all(gates[g] == "passed" for g in ("G1", "G3", "G4")), result.text()
            assert result.status in (lifecycle.VERIFIED_BASIC, lifecycle.PENDING_APPROVAL), result.text()
    finally:
        runner.cleanup()
