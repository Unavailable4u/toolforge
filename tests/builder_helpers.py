"""Shared by the builder tests: model replies that are packages, a gate whose result changes from call to call, and a ready builder.

Everything here is a stand-in for something real: the model is `FakeLLM` (scripted replies, same scanning and parsing as the real
client), the registry is real, and the gates are scripted, so no test needs Docker, a network or a clock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from verify_helpers import BuildingGate, MemoryLedgers, ScriptedGate, valid_package

from toolforge.config import Settings
from toolforge.llm.client import ChatReply, FakeLLM
from toolforge.registry import Registry
from toolforge.services.builder import Builder
from toolforge.services.forge import Forge
from toolforge.verify.g1_static import StaticGate
from toolforge.verify.pipeline import GateContext, GateResult, passed

FACTS = {
    "python": "3.12.4",
    "runtime_image": "python:3.12-slim",
    "limits": {"default_timeout_s": 20},
    "libraries": [{"name": "numpy", "version": "2.0.0"}],
}
GOAL = "count the words in a piece of text"


def package_reply(name: str = "my_tool", **manifest_changes: Any) -> dict[str, Any]:
    """What a model answers: `{manifest, files}` with no `version` and no `parent_version`, as the builder's prompt asks."""
    package = valid_package(name=name, **manifest_changes)
    manifest = package.manifest.model_dump(mode="json")
    manifest.pop("version", None)
    manifest.pop("parent_version", None)
    return {"manifest": manifest, "files": dict(package.files)}


class SequenceGate:
    """A gate that answers from a list, one result per call, and repeats the last one when the list runs out."""

    budget_s = 60

    def __init__(self, gate_id: str, results: list[GateResult]) -> None:
        self.id = gate_id
        self.results = list(results)
        self.calls = 0

    def run(self, ctx: GateContext) -> GateResult:
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[index]


def gates_with(g3: SequenceGate | ScriptedGate | None = None, *, real_g1: bool = False) -> list[Any]:
    """G1 to G4 as scripted gates (G2 stores the blob). `real_g1` keeps the real structural gate, which rejects an invalid package."""
    return [
        StaticGate() if real_g1 else ScriptedGate("G1"),
        BuildingGate(),
        g3 or ScriptedGate("G3"),
        ScriptedGate("G4"),
    ]


class NoUsageLLM(FakeLLM):
    """A provider that reports no token counts: the build must say "not measured" and count the budget with an estimate."""

    def chat(self, messages, *, deadline=None) -> ChatReply:
        reply = super().chat(messages, deadline=deadline)
        return ChatReply(
            reply.text, reply.model, None, None, reply.latency_ms, reply.attempts, reply.finish_reason
        )


class Rig:
    """A builder over a real registry in a temporary folder."""

    def __init__(
        self,
        tmp_path: Path,
        llm: FakeLLM | None,
        gates: list[Any] | None = None,
        *,
        facts: dict[str, Any] | None = FACTS,
        **settings_options: Any,
    ) -> None:
        self.settings = Settings(home=tmp_path / "home", **settings_options)
        self.registry = Registry(self.settings.home)
        self.ledgers = MemoryLedgers()
        self.llm = llm
        self.gates = gates if gates is not None else gates_with()
        self.forge = Forge(self.settings, self.registry, None, gates=self.gates)
        self.forge._pipeline._approval._brokers = (
            lambda: {}
        )  # no MiniLocker: there are no brokered services to look up
        self.builder = Builder(
            self.settings,
            self.registry,
            self.forge,
            client=llm,
            ledger_factory=self.ledgers,
            runtime_facts=(lambda: facts) if facts is not None else None,
        )
        self.notes: list[str] = []

    def build(self, goal: str = GOAL, **options: Any):
        return self.builder.build(goal, on_progress=self.notes.append, **options)

    def close(self) -> None:
        self.registry.close()


__all__ = [
    "FACTS",
    "GOAL",
    "NoUsageLLM",
    "Rig",
    "SequenceGate",
    "gates_with",
    "package_reply",
    "passed",
]
