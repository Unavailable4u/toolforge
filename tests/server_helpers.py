"""Builders shared by the MCP server tests: an in-memory server over a real registry, scripted gates and a stand-in tool runner."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from verify_helpers import MemoryLedgers, ScriptedGate, scripted_gates, valid_package

from toolforge.config import Settings
from toolforge.models.results import CallError, CallResult
from toolforge.registry import Registry
from toolforge.server.app import ForgeApp, build_server
from toolforge.services import operator_actions
from toolforge.services.builder import Builder
from toolforge.services.forge import Forge


class StandInRunner:
    """Replaces `ToolRunner`: remembers each call and returns the scripted `CallResult`. Runs no code."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: CallResult | Exception = CallResult(
            ok=True, result={"answer": 42}, duration_ms=3, call_id="stand-in"
        )

    def run(self, record, args, *, files=None, allow_status=None, **kwargs) -> CallResult:
        self.calls.append({"label": record.label, "args": args, "files": files, "allow_status": allow_status})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    @staticmethod
    def failure(kind: str, message: str, **fields: Any) -> CallResult:
        return CallResult(
            ok=False, error=CallError(type=kind, message=message), duration_ms=1, call_id="x", **fields
        )


@dataclass
class Env:
    settings: Settings
    registry: Registry
    app: ForgeApp
    ledgers: MemoryLedgers
    runner: StandInRunner
    gates: list[ScriptedGate] = field(default_factory=list)

    def add_tool(self, *, promote: bool = True, **package_args: Any) -> str:
        """Verify (scripted gates) and, by default, promote a tool. Returns `name@version`."""
        forge = Forge(self.settings, self.registry, None, gates=scripted_gates())
        report = forge.submit_package(valid_package(**package_args), "agent")
        assert report.registered, report.text()
        ref = f"{report.name}@{report.version}"
        if promote:
            operator_actions.promote(self.settings, self.registry, ref, "test setup")
        return ref

    def call(self, tool: str, arguments: dict[str, Any] | None = None, **options: Any):
        async def go(client: Client):
            return await client.call_tool(tool, arguments or {}, **options)

        return self.session(go)

    def session(self, work: Callable[[Client], Awaitable[Any]], **client_options: Any) -> Any:
        async def main():
            async with Client(self.app.server, **client_options) as client:
                return await work(client)

        return anyio.run(main)


def make_env(
    tmp_path: Path, *, gates: list[ScriptedGate] | None = None, llm: Any = None, **settings_options: Any
) -> Env:
    settings = Settings(home=tmp_path / "home", **settings_options)
    registry = Registry(settings.home)
    ledgers = MemoryLedgers()
    runner = StandInRunner()
    gate_list = gates if gates is not None else scripted_gates()
    forge = Forge(settings, registry, None, gates=gate_list, queue_timeout_s=0.2)
    builder = (
        Builder(
            settings,
            registry,
            forge,
            client=llm,
            ledger_factory=ledgers,
            runtime_facts=lambda: {"python": "3.12.4", "limits": {"default_timeout_s": 20}},
        )
        if llm is not None
        else None
    )
    app = build_server(
        settings,
        registry=registry,
        sandbox=object(),
        ledger_factory=ledgers,
        forge=forge,
        tool_runner=runner,  # type: ignore[arg-type]
        runtime_probe=lambda image: {
            "python": "3.12.4",
            "libraries": [["numpy", "2.0.0"], ["pandas", "2.2.2"]],
        },
        builder=builder,
        watch_interval_s=0.05,
    )
    return Env(settings, registry, app, ledgers, runner, gate_list)


def payload_of(package) -> dict[str, Any]:
    return {"manifest": package.manifest.model_dump(mode="json"), "files": dict(package.files)}


def data(result) -> dict[str, Any]:
    """The structured content of a tool result; fails clearly when the call was an error."""
    assert not result.is_error, result.content[0].text
    return result.structured_content


def error_text(result) -> str:
    assert result.is_error, result.structured_content
    return result.content[0].text


def text_of(result) -> str:
    return json.dumps(result.structured_content) if result.structured_content else result.content[0].text
