"""Small builders shared by the verification tests: valid packages, a ledger that keeps events in memory, scripted gates."""

from __future__ import annotations

import copy
from typing import Any

from toolforge.cli_package import scaffold
from toolforge.models.package import ToolPackage
from toolforge.verify.pipeline import GateContext, GateResult, passed


def valid_package(
    name: str = "my_tool",
    version: str = "0.1.0",
    *,
    tool: str | None = None,
    tests: str | None = None,
    extra_files: dict[str, str] | None = None,
    **manifest_changes: Any,
) -> ToolPackage:
    """A package that passes `validate`, built from the scaffold. `tool` and `tests` replace the sources; other keywords
    replace manifest fields (`permissions={...}`, `examples=[...]`, `requirements=[...]`). The scaffold's own tests assert on values.
    """
    base = scaffold(name)
    manifest = copy.deepcopy(base.manifest.model_dump(mode="json"))
    manifest["version"] = version
    manifest.update(manifest_changes)
    files = dict(base.files)
    if tool is not None:
        files["tool.py"] = tool
    if tests is not None:
        files["tests/test_tool.py"] = tests
    files.update(extra_files or {})
    return ToolPackage(manifest, files)


class MemoryLedgers:
    """A `ledger_factory` for tests: remembers every `(task_id, actor, type, payload)` and writes no file."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []
        self.paths: list[str | None] = []

    def __call__(self, path: str | None, task_id: str) -> MemoryLedgers._Writer:
        self.paths.append(path)
        return MemoryLedgers._Writer(self, task_id)

    class _Writer:
        def __init__(self, owner: MemoryLedgers, task_id: str) -> None:
            self.owner, self.task_id = owner, task_id

        def append(self, actor: str, type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
            self.owner.events.append((self.task_id, actor, type, dict(payload or {})))
            return {}

    @property
    def types(self) -> list[str]:
        return [type for _, _, type, _ in self.events]


class ScriptedGate:
    """A gate that returns a fixed result, or raises, and notes that it ran. `budget_s` is what the pipeline enforces."""

    def __init__(
        self,
        gate_id: str,
        result: GateResult | None = None,
        *,
        raises: BaseException | None = None,
        budget_s: int = 60,
        action: Any = None,
    ) -> None:
        self.id = gate_id
        self.budget_s = budget_s
        self.result = result or passed(f"{gate_id} is fine")
        self.raises = raises
        self.action = action
        self.calls = 0

    def run(self, ctx: GateContext) -> GateResult:
        self.calls += 1
        if self.action is not None:
            self.action(ctx)
        if self.raises is not None:
            raise self.raises
        return self.result
