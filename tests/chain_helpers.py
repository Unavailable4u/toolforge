"""A rig for chain tests: a REAL registry and the REAL `ToolRunner`, over a scripted sandbox that answers from a table (no tool code runs).

`ChainRig.answers[tool name]` is what the sandbox prints for that tool: a JSON value, a function `args -> JSON value`, or a `RunOutput`.
`rig.seen` lists every call the sandbox answered as `(tool, version, args)`, in order, and `rig.sandbox.opened` every sandbox that was
opened: a chain that is refused before step 1 leaves both empty. `rig.before_answer` runs just before a call is answered, so a test can
change the registry in the middle of a chain.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sandbox_scripts import envelope
from verify_helpers import MemoryLedgers, scripted_gates, valid_package

from toolforge.config import Settings
from toolforge.registry import Registry, VersionRecord, lifecycle
from toolforge.sandbox import FakeRunner, RunOutput
from toolforge.services import operator_actions
from toolforge.services import runner as runner_module
from toolforge.services.chain import ChainRunner
from toolforge.services.forge import Forge
from toolforge.services.runner import ToolRunner

NO_PERMISSIONS = {"network": [], "secrets": [], "inputs": False, "browser": False}
# A schema loose enough for any test value in these properties (the package rules demand `additionalProperties: false`, so the names
# are listed); a tool that needs to refuse a value passes its own `input_schema`.
LOOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "rows": {},
        "v": {},
        "w": {},
        "note": {},
        "style": {"type": "string"},
    },
    "additionalProperties": False,
}


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ChainRig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> None:
        monkeypatch.setattr(
            runner_module, "ARG_CHECK_ISOLATED", False
        )  # the schema check in-process: fast, and the schemas are ours
        settings.setdefault("chain_enabled", True)
        self.settings = Settings(home=tmp_path / "home", **settings)
        self.registry = Registry(self.settings.home)
        self.ledgers = MemoryLedgers()
        self.clock = FakeClock()
        self.answers: dict[str, Any] = {}
        self.seen: list[tuple[str, str, dict[str, Any]]] = []
        self.before_answer: Callable[[str, str, dict[str, Any]], None] | None = None
        self.sandbox = FakeRunner(self._respond)
        self.forge = Forge(self.settings, self.registry, None, gates=scripted_gates())
        self.runner = ToolRunner(
            self.sandbox, self.registry, self.settings, self.ledgers, session_reaper=False
        )
        self.chains = ChainRunner(
            self.settings, self.registry, self.runner, ledger_factory=self.ledgers, clock=self.clock
        )

    # -- the scripted sandbox ---------------------------------------------------------------------------------------------------

    def _respond(self, argv: list[str], put: dict[str, bytes]) -> RunOutput:
        manifest = json.loads(put["/workspace/pkg/manifest.json"])
        request = json.loads(put["/workspace/.tf/in.json"])
        name, version, args = manifest["name"], manifest["version"], request["args"]
        self.seen.append((name, version, args))
        if self.before_answer is not None:
            self.before_answer(name, version, args)
        answer = self.answers.get(name, {})
        if callable(answer):
            answer = answer(args)
        if isinstance(answer, RunOutput):
            return answer
        return RunOutput(0, envelope(argv[-1], answer), b"", False, False)

    # -- making tools -----------------------------------------------------------------------------------------------------------

    def _package(self, name: str, version: str, **manifest: Any):  # noqa: ANN202
        manifest.setdefault("input_schema", LOOSE_SCHEMA)
        manifest.setdefault("output_schema", None)
        manifest.setdefault("examples", [{"input": {"text": "hi"}}])
        return valid_package(name, version, **manifest)

    def add_tool(
        self, name: str, version: str = "0.1.0", *, promote: bool = True, **manifest: Any
    ) -> VersionRecord:
        """A verified (scripted gates) tool, promoted to active unless `promote=False`."""
        report = self.forge.submit_package(self._package(name, version, **manifest), "agent")
        assert report.registered and report.ok, report.text()
        if promote:
            operator_actions.promote(self.settings, self.registry, f"{name}@{version}", "test setup")
        record = self.registry.find_version(name, version)
        assert record is not None
        return record

    def add_with_permissions(
        self, name: str, permissions: dict[str, Any], *, approve: bool, brokers: dict[str, str] | None = None
    ) -> VersionRecord:
        """A tool that asks for network hosts or secrets: submitted, and approved (so it is active) when `approve` is set."""
        if brokers:
            self.forge._pipeline._approval._brokers = lambda: brokers
        report = self.forge.submit_package(self._package(name, "0.1.0", permissions=permissions), "agent")
        assert report.status == lifecycle.PENDING_APPROVAL, report.text()
        if approve:
            operator_actions.decide(
                self.settings,
                self.registry,
                report.approval["id"],
                "approve",
                "test setup",
                brokers=(lambda: brokers) if brokers else None,
                ledger_factory=self.ledgers,
            )
        record = self.registry.find_version(name, "0.1.0")
        assert record is not None
        return record

    def add_raw(self, name: str, *, blob: bool = True, build_gate: bool = True) -> VersionRecord:
        """An active version made straight in the registry, so a test can leave out the blob or the G2 row that `promote` would demand."""
        package = self._package(name, "0.1.0")
        record = self.registry.add_version(package, created_by="test")
        self.registry.transition(record.id, lifecycle.VERIFYING, actor="pipeline")
        if blob:
            self.registry.attach_blob(record.id, self.registry.blobs.put(package.to_tar()))
        if build_gate:
            self.registry.record_gate(record.id, "G2", "passed", {})
        for status in (lifecycle.VERIFIED, lifecycle.ACTIVE):
            self.registry.transition(record.id, status, actor="pipeline")
        found = self.registry.get_version(record.id)
        assert found is not None and found.status == lifecycle.ACTIVE
        return found

    # -- reading what happened --------------------------------------------------------------------------------------------------

    def chain_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (type_, payload)
            for _, _, type_, payload in self.ledgers.events
            if type_.startswith("forge.chain.")
        ]

    def events(self, type_: str) -> list[dict[str, Any]]:
        return [payload for _, _, kind, payload in self.ledgers.events if kind == type_]

    def close(self) -> None:
        self.runner.sessions.shutdown()
        self.registry.close()


def step(step_id: str, tool: str, **args: Any) -> dict[str, Any]:
    """`{"id": ..., "tool": ..., "args": {...}}`; use `ref("a.output.rows")` for a value that comes from an earlier step."""
    return {"id": step_id, "tool": tool, "args": args}


def ref(text: str) -> dict[str, str]:
    return {"$ref": text}
