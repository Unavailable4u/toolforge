"""The benchmark agent loop (P16) with a scripted fake LLM and fake backends, plus the two real backends without Docker.

`McpBackend` is driven against the real ToolForge server in memory (`server_helpers.make_env`), and `PythonBackend` against the real
`ToolRunner` over `local_sandbox.LocalRunner` (no isolation; it runs only the code this test wrote).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from local_sandbox import LocalRunner
from server_helpers import make_env
from verify_helpers import MemoryLedgers

from bench import agent as agent_module
from bench import tasks as task_set
from bench.agent import Agent, Budget, McpBackend, PythonBackend, ToolReply, ToolSpec
from toolforge.config import Settings
from toolforge.errors import LLMRefused, LLMUnavailable
from toolforge.llm.client import ChatReply
from toolforge.services.runner import ToolRunner


def reply(value: Any, *, usage: bool = True) -> ChatReply:
    text = value if isinstance(value, str) else json.dumps(value)
    return ChatReply(
        text=text,
        model="scripted-1",
        prompt_tokens=100 if usage else None,
        completion_tokens=10 if usage else None,
        latency_ms=1,
        attempts=1,
    )


class ScriptedModel:
    """Returns the scripted replies in order; an exception in the script is raised instead. Remembers every request."""

    model = "scripted-1"

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]], *, deadline: float | None = None) -> ChatReply:
        self.requests.append([dict(m) for m in messages])
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, ChatReply) else reply(item)


class FakeBackend:
    files_note = "the fake backend sees files only when attached"

    def __init__(self, answer: str = '{"ok": true}', error: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answer, self.error = answer, error

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec("echo", "Echo the arguments back.", {"type": "object"})]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolReply:
        self.calls.append((name, arguments))
        return ToolReply(self.answer, self.error, {"echo": arguments})


def call(tool: str = "echo", **arguments: Any) -> dict[str, Any]:
    return {"action": "call", "tool": tool, "arguments": arguments}


def final(answer: Any) -> dict[str, Any]:
    return {"action": "final", "answer": answer}


def test_a_final_answer_ends_the_run_and_usage_is_summed() -> None:
    model = ScriptedModel(call(x=1), final({"n": 3}))
    result = Agent(model, FakeBackend(), files={}).run("task")
    assert result.stop_reason == "answered" and result.answered and result.answer == {"n": 3}
    assert result.steps == 1 and result.requests == 2
    assert result.tokens_in == 200 and result.tokens_out == 20 and result.tokens == 220
    assert result.model == "scripted-1" and result.calls[0].ok


def test_files_are_put_in_where_the_agent_writes_a_placeholder_and_authored_keeps_the_placeholder() -> None:
    backend = FakeBackend()
    files = {"data.txt": b"hello", "blob.bin": b"\x00\x01\xff"}
    model = ScriptedModel(
        call(files={"data.txt": {"$file": "data.txt"}, "b": {"$file": "inputs/blob.bin"}}), final(1)
    )
    result = Agent(model, backend, files=files).run("task")
    sent = backend.calls[0][1]["files"]
    assert sent["data.txt"] == "hello" and sent["b"] == {"base64": base64.b64encode(b"\x00\x01\xff").decode()}
    assert '"$file": "data.txt"' in result.calls[0].authored and "hello" not in result.calls[0].authored


def test_an_unknown_file_or_tool_is_reported_to_the_agent_and_counts_as_a_step() -> None:
    backend = FakeBackend()
    model = ScriptedModel(call(f={"$file": "nope.txt"}), call("no_such_tool"), final(0))
    result = Agent(model, backend, files={}).run("task")
    assert backend.calls == [] and result.steps == 2 and not any(c.ok for c in result.calls)
    messages = model.requests[-1]
    assert "no input file called nope.txt" in messages[-3]["content"]
    assert "no tool called no_such_tool" in messages[-1]["content"]


def test_one_bad_reply_gets_a_nudge_and_two_in_a_row_stop_the_run() -> None:
    model = ScriptedModel("not json", final(5))
    assert Agent(model, FakeBackend(), files={}).run("t").answer == 5
    assert "could not be used" in model.requests[1][-1]["content"]
    model = ScriptedModel("not json", {"action": "dance"})
    result = Agent(model, FakeBackend(), files={}).run("t")
    assert result.stop_reason == "bad_reply" and not result.answered and result.bad_replies == 2


def test_the_step_budget_stops_a_call_past_the_limit() -> None:
    backend = FakeBackend()
    model = ScriptedModel(call(), call(), call(), final(1))
    result = Agent(model, backend, files={}, budget=Budget(max_steps=2)).run("t")
    assert result.stop_reason == "step_budget" and len(backend.calls) == 2 and not result.answered


def test_the_token_budget_uses_reported_usage_and_falls_back_to_the_estimate() -> None:
    result = Agent(
        ScriptedModel(call(), call(), final(1)), FakeBackend(), files={}, budget=Budget(max_tokens=150)
    ).run("t")
    assert (
        result.stop_reason == "token_budget" and result.tokens == 220
    )  # checked after each reply: 110, then 220 > 150
    assert result.requests == 2 and not result.answered
    model = ScriptedModel(reply(call(), usage=False), reply(final(1), usage=False))
    result = Agent(model, FakeBackend(), files={}).run("t")
    assert (
        result.tokens_in is None and result.tokens is None and result.tokens_estimated > 0
    )  # not measured, never zero


def test_the_time_budget() -> None:
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    model = ScriptedModel(call(), final(1))
    result = Agent(
        model, FakeBackend(), files={}, budget=Budget(max_seconds=5), clock=lambda: next(ticks)
    ).run("t")
    assert result.stop_reason == "time_budget"


@pytest.mark.parametrize(
    ("error", "reason"), [(LLMRefused("secret"), "llm_refused"), (LLMUnavailable("down"), "llm_unavailable")]
)
def test_client_errors_stop_the_run_with_a_reason(error: Exception, reason: str) -> None:
    result = Agent(ScriptedModel(error), FakeBackend(), files={}).run("t")
    assert result.stop_reason == reason and not result.answered


def test_the_system_prompt_is_one_template_with_only_the_tools_and_file_note_changing() -> None:
    files = {"a.csv": b"x"}
    forge = Agent(ScriptedModel(), FakeBackend(), files=files).system_prompt(FakeBackend().tools())
    baseline_backend = PythonBackend(object(), files)
    baseline = Agent(ScriptedModel(), baseline_backend, files=files).system_prompt(baseline_backend.tools())
    head = agent_module.SYSTEM_PROMPT.split("Input files")[0].replace("{{", "{").replace("}}", "}")
    assert forge.startswith(head) and baseline.startswith(head)
    assert "a.csv" in forge and "a.csv" in baseline and "at most 20 tool calls" in baseline
    assert agent_module.prompt_hash() == agent_module.prompt_hash() and len(agent_module.prompt_hash()) == 16


def test_tool_output_reaches_the_model_capped() -> None:
    backend = FakeBackend(answer="y" * (agent_module.MAX_RESULT_CHARS + 500))
    model = ScriptedModel(call(), final(1))
    result = Agent(model, backend, files={}).run("t")
    assert "(cut: the result had" in model.requests[1][-1]["content"]
    assert result.calls[0].result_chars < agent_module.MAX_RESULT_CHARS + 200


def test_a_run_record_holds_sizes_and_hashes_not_arguments() -> None:
    result = Agent(ScriptedModel(call(secret="classified"), final(1)), FakeBackend(), files={}).run("t")
    text = json.dumps(result.record())
    assert "classified" not in text and result.record()["calls"][0]["arguments_sha256"]


def test_the_baseline_runs_the_agents_code_through_the_real_tool_runner_and_solves_a_task(
    tmp_path: Path,
) -> None:
    instance = task_set.get("log_stats").generate(3)
    code = (
        "import json\nstats = {}\n"
        "for line in open('inputs/service.log'):\n"
        "    line = line.rstrip('\\n')\n"
        "    if not line or line.startswith(('##', '>>')):\n        continue\n"
        "    _, _, kv = line.split('|', 2)\n"
        "    d = dict(p.split('=', 1) for p in kv.split(';'))\n"
        "    lat = d['lat']\n"
        "    us = int(lat[:-2]) * 1000 if lat.endswith('ms') else int(lat[:-2])\n"
        "    e = stats.setdefault(d['svc'], {'count': 0, 'errors': 0, 'max_lat_us': 0})\n"
        "    e['count'] += 1; e['errors'] += int(d['st']) >= 500; e['max_lat_us'] = max(e['max_lat_us'], us)\n"
        "print(json.dumps({'services': stats}))\n"
    )

    class Solver(ScriptedModel):
        def chat(self, messages, *, deadline=None):
            self.requests.append(messages)
            if len(self.requests) == 1:
                return reply(call("run_python", code=code))
            response = json.loads(messages[-1]["content"].split("\n", 1)[1])
            assert (
                response["tool"] == "run_python" and "UNTRUSTED" in response["output"]
            )  # fenced like forge_run
            body = response["output"].split(">>>\n", 1)[1].rsplit("\n<<<", 1)[0]
            return reply(final(json.loads(json.loads(body)["stdout"])))

    runner = ToolRunner(LocalRunner(), None, Settings(home=tmp_path / "home"), MemoryLedgers())
    result = Agent(Solver(), PythonBackend(runner, instance.files), files=instance.files).run(instance.prompt)
    assert result.stop_reason == "answered" and result.calls[0].ok
    assert task_set.get("log_stats").check(instance, result.answer, {}).ok


def test_the_baseline_package_is_valid_and_its_code_error_comes_back_to_the_agent(tmp_path: Path) -> None:
    package = agent_module.run_python_package()
    assert package.validate(Settings(home=tmp_path / "v")).ok
    runner = ToolRunner(LocalRunner(), None, Settings(home=tmp_path / "home"), MemoryLedgers())
    backend = PythonBackend(runner, {})
    answer = backend.call("run_python", {"code": "raise ValueError('boom')"})
    assert "ValueError" in answer.text and "boom" in answer.text
    assert backend.call("other", {}).is_error


def test_the_mcp_backend_drives_the_real_server_through_the_official_client(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.add_tool(name="word_counter", description="Count the words in a piece of text and return the total.")
    with McpBackend(env.app.server) as backend:
        names = [t.name for t in backend.tools()]
        assert "forge_find" in names and "forge_run" in names
        found = backend.call("forge_find", {"query": "count words"})
        assert not found.is_error and found.data["results"][0]["name"] == "word_counter"
        assert "word_counter" in found.text
        refused = backend.call("forge_describe", {"name": "nothing_here"})
        assert refused.is_error
