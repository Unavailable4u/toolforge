"""What the server's answers cost in a model's context, measured instead of assumed (`python -m bench context`).

For each registry size, a seeded synthetic registry (`bench/synthetic.py`) is served by the **real** server, in memory, through the
official MCP client, and three things are recorded:

* `meta`: the default server's `tools/list` payload, one `forge_find` at the default limit, and one `forge_describe` of the top hit (the
  "find, read, use" path a model really takes);
* `direct`: the `tools/list` payload with `expose_direct` on, which is what a server that lists every tool costs;
* `chain` (only when S02's `forge_chain` exists): the request and response of a three-step chain against the same three steps as
  separate `forge_run` calls. Runs are answered by a stand-in runner, so no tool is executed and nothing needs Docker.

Every figure is bytes (UTF-8 of the JSON the client receives, compact, as `server/budget.py::size_of` writes it) and tokens. Tokens are
`budget.estimate_tokens` with `"method": "estimate"`, unless an endpoint is given: then each payload is sent once with and once without
(tool definitions as `tools`, a response as an extra message) in a request with `max_tokens` 1, and the difference in
`usage.prompt_tokens` is recorded with `"method": "measured"` and the model id. A payload the endpoint cannot count is `null`, never 0.

These numbers describe synthetic tools of the stated sizes and one tokenizer (or the four-characters estimate). They say nothing about
any other system.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from bench import synthetic
from toolforge.config import Settings
from toolforge.llm.client import HttpResponse, Transport, UrllibTransport, refuse_secrets
from toolforge.models.results import CallResult
from toolforge.server import budget
from toolforge.server.tools import META_TOOLS, OPTIONAL_TOOLS

DEFAULT_SIZES = (10, 100, 1000)
FIND_QUERY = "count the words in a piece of text"
CHAIN_TEXT = "  Ledger entries   for the  quarter, cleaned and counted.  "
ENDPOINT_TIMEOUT_S = 60.0
BASE_MESSAGE = [{"role": "user", "content": "Reply with the single word: ok"}]


class _StandInRunner:
    """Answers `forge_run` and chain steps without running anything: the tools are synthetic and only their replies' size matters."""

    def run(self, record: Any, args: Any, **_: Any) -> CallResult:
        text = " ".join(str((args or {}).get("text", "")).split())
        return CallResult(
            ok=True, result={"text": text, "count": len(text.split())}, duration_ms=1, call_id="stand-in"
        )


class _NoLedger:
    def __call__(self, path: str | None, task_id: str) -> _NoLedger:
        return self

    def append(self, actor: str, type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}


def chain_available() -> bool:
    """True when S02 (`forge_chain`) is built."""
    return "forge_chain" in OPTIONAL_TOOLS and any(tool.name == "forge_chain" for tool in META_TOOLS)


# --- Token counting ---------------------------------------------------------------------------------------------------------


class Counter:
    """Turns a payload into a token figure. The default estimates; `EndpointCounter` measures."""

    method = "estimate"
    model: str | None = None

    def tools(self, tools: list[dict[str, Any]], text: str) -> int | None:
        return budget.estimate_tokens(text)

    def message(self, text: str) -> int | None:
        return budget.estimate_tokens(text)


def openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP tool definitions in the OpenAI `tools` shape, which is how a client hands them to a model."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema", {"type": "object"}),
            },
        }
        for tool in tools
    ]


class EndpointCounter(Counter):
    """Counts with a real OpenAI-compatible endpoint: `usage.prompt_tokens` with the payload minus without it.

    Uses the LLM client's transport (no redirects, size cap) and its secret check: a payload with secret-shaped text is not sent.
    """

    method = "measured"

    def __init__(
        self, base_url: str, model: str, api_key: str | None = None, *, transport: Transport | None = None
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("the endpoint must be an http or https URL")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self._key = api_key
        self._transport = transport or UrllibTransport()
        self._baseline: int | None = None

    def _prompt_tokens(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None
    ) -> int | None:
        refuse_secrets(messages + [{"content": json.dumps(tools or [])}])
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": 1}
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        response: HttpResponse = self._transport.post(
            self.url, headers, json.dumps(body).encode("utf-8"), ENDPOINT_TIMEOUT_S
        )
        if response.status != 200:
            return None
        try:
            usage = json.loads(response.body.decode("utf-8")).get("usage") or {}
        except (ValueError, UnicodeDecodeError, AttributeError):
            return None
        value = usage.get("prompt_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _base(self) -> int | None:
        if self._baseline is None:
            self._baseline = self._prompt_tokens(list(BASE_MESSAGE), None)
        return self._baseline

    def tools(self, tools: list[dict[str, Any]], text: str) -> int | None:
        base, with_payload = self._base(), self._prompt_tokens(list(BASE_MESSAGE), openai_tools(tools))
        return None if base is None or with_payload is None else with_payload - base

    def message(self, text: str) -> int | None:
        base = self._base()
        with_payload = self._prompt_tokens(list(BASE_MESSAGE) + [{"role": "user", "content": text}], None)
        return None if base is None or with_payload is None else with_payload - base


# --- Measuring --------------------------------------------------------------------------------------------------------------


def _figure(text: str, tokens: int | None) -> dict[str, Any]:
    return {"bytes": len(text.encode("utf-8")), "chars": len(text), "tokens": tokens}


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _sum(*figures: dict[str, Any]) -> dict[str, Any]:
    tokens = [f["tokens"] for f in figures]
    return {
        "bytes": sum(f["bytes"] for f in figures),
        "chars": sum(f["chars"] for f in figures),
        "tokens": None if any(t is None for t in tokens) else sum(tokens),
    }


def _serve(settings: Settings, registry: Any, work: Callable[[Any], Any]) -> Any:
    import anyio
    from mcp import Client

    from toolforge.server.app import build_server
    from toolforge.services.forge import Forge

    ledger = _NoLedger()
    app = build_server(
        settings,
        registry=registry,
        sandbox=object(),
        ledger_factory=ledger,
        forge=Forge(settings, registry, None, gates=[]),
        tool_runner=_StandInRunner(),  # type: ignore[arg-type]
        runtime_probe=lambda image: {"python": "3.12", "libraries": []},
    )

    async def main() -> Any:
        async with Client(app.server) as client:
            return await work(client)

    try:
        return anyio.run(main)
    finally:
        app.close()


async def _tools_list(client: Any) -> list[dict[str, Any]]:
    listed = await client.list_tools()
    return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in listed.tools]


def _response(result: Any) -> dict[str, Any]:
    if result.is_error:
        text = " ".join(getattr(block, "text", "") for block in result.content)
        raise RuntimeError(f"the server refused a measurement call: {text[:300]}")
    return result.structured_content or {}


def measure_size(home: Path, size: int, seed: int, counter: Counter) -> dict[str, Any]:
    """Measure one registry size. `home` must be an empty directory."""
    registry, items = synthetic.build(home, size, seed)
    try:
        plain = Settings(home=home)

        async def meta(client: Any) -> dict[str, Any]:
            tools = await _tools_list(client)
            found = _response(await client.call_tool("forge_find", {"query": FIND_QUERY}))
            top = found["results"][0]["name"] if found.get("results") else None
            described = _response(await client.call_tool("forge_describe", {"name": top})) if top else None
            return {"tools": tools, "find": found, "describe": described, "top": top}

        got = _serve(plain, registry, meta)
        tools_text = _compact(got["tools"])
        find_text, describe_text = _compact(got["find"]), _compact(got["describe"])
        meta_block = {
            "tools_list": _figure(tools_text, counter.tools(got["tools"], tools_text)),
            "find": _figure(find_text, counter.message(find_text)),
            "describe": _figure(describe_text, counter.message(describe_text)) if got["describe"] else None,
            "tools_listed": len(got["tools"]),
            "find_results": len(got["find"].get("results", [])),
            "top_hit": got["top"],
            "find_budget_chars": budget.BUDGET.find_chars,
            "find_within_budget": len(find_text) <= budget.BUDGET.find_chars,
        }
        parts = [meta_block["tools_list"], meta_block["find"]] + (
            [meta_block["describe"]] if meta_block["describe"] else []
        )
        meta_block["total"] = _sum(*parts)

        direct_tools = _serve(Settings(home=home, expose_direct=True), registry, _tools_list)
        direct_text = _compact(direct_tools)
        direct_block = {
            "tools_list": _figure(direct_text, counter.tools(direct_tools, direct_text)),
            "tools_listed": len(direct_tools),
        }

        chain_block = None
        if chain_available():
            names = list(synthetic.chain_names(items))[:3]
            if len(names) == 3:
                chain_block = _measure_chain(home, registry, names, counter)
        return {"size": size, "meta": meta_block, "direct": direct_block, "chain": chain_block}
    finally:
        registry.close()


def _measure_chain(home: Path, registry: Any, names: list[str], counter: Counter) -> dict[str, Any]:
    steps = [{"id": "s1", "tool": names[0], "args": {"text": CHAIN_TEXT}}]
    steps += [
        {"id": f"s{i + 1}", "tool": name, "args": {"text": {"$ref": f"s{i}.output.text"}}}
        for i, name in enumerate(names[1:], start=1)
    ]
    chain_request = {"name": "forge_chain", "arguments": {"steps": steps}}

    async def work(client: Any) -> dict[str, Any]:
        chained = _response(await client.call_tool("forge_chain", chain_request["arguments"]))
        separate = []
        text = CHAIN_TEXT
        for name in names:
            request = {"name": "forge_run", "arguments": {"name": name, "args": {"text": text}}}
            response = _response(await client.call_tool("forge_run", request["arguments"]))
            separate.append((request, response))
            text = " ".join(
                text.split()
            )  # what the stand-in returns; a model would copy it into the next call
        return {"chain": chained, "separate": separate}

    got = _serve(Settings(home=home, chain_enabled=True), registry, work)
    request_text, response_text = _compact(chain_request), _compact(got["chain"])
    chain = {
        "request": _figure(request_text, counter.message(request_text)),
        "response": _figure(response_text, counter.message(response_text)),
    }
    chain["total"] = _sum(chain["request"], chain["response"])
    separate_parts = []
    for request, response in got["separate"]:
        for value in (request, response):
            text = _compact(value)
            separate_parts.append(_figure(text, counter.message(text)))
    return {"steps": 3, "chain": chain, "separate_runs": {"calls": 3, "total": _sum(*separate_parts)}}


def measure(sizes: Sequence[int], seed: int, counter: Counter | None = None) -> dict[str, Any]:
    """The `context` block of `summary.json`."""
    counter = counter or Counter()
    if not sizes or any(size < 1 for size in sizes):
        raise ValueError("sizes must be positive numbers of tools")
    results = []
    for size in sizes:
        with tempfile.TemporaryDirectory(prefix="toolforge-bench-context-") as tmp:
            results.append(measure_size(Path(tmp), size, seed, counter))
    return {
        "seed": seed,
        "sizes": list(sizes),
        "method": counter.method,
        "model": counter.model,  # None for the estimate: no tokenizer was used
        "tokens_note": (
            "four characters a token (server/budget.py::estimate_tokens), not a tokenizer"
            if counter.method == "estimate"
            else f"usage.prompt_tokens difference reported by {counter.model}"
        ),
        "find_query": FIND_QUERY,
        "registry": "synthetic: tools generated from templates, registered with scripted gate results (bench/synthetic.py)",
        "chain_measured": chain_available(),
        "results": results,
        "hypotheses": hypotheses(results),
    }


def hypotheses(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The two stated hypotheses (docs/BENCHMARK.md), checked against the numbers whichever way they fall."""
    if len(results) < 2:
        return {"meta_tools_list_constant": None, "find_within_budget": None, "direct_grows_with_size": None}
    lists = {r["meta"]["tools_list"]["bytes"] for r in results}
    direct = [r["direct"]["tools_list"]["bytes"] for r in results]
    # Bytes each registry tool adds to the direct listing (the direct payload minus the meta tools, over the size): a steady figure
    # across sizes means the growth is proportional to the number of tools.
    per_tool = [
        round((r["direct"]["tools_list"]["bytes"] - r["meta"]["tools_list"]["bytes"]) / r["size"], 1)
        for r in results
    ]
    return {
        "meta_tools_list_constant": len(lists) == 1,
        "find_within_budget": all(r["meta"]["find_within_budget"] for r in results),
        "direct_grows_with_size": all(a < b for a, b in zip(direct, direct[1:], strict=False)),
        "direct_bytes_per_tool": per_tool,
    }


__all__ = [
    "DEFAULT_SIZES",
    "Counter",
    "EndpointCounter",
    "chain_available",
    "hypotheses",
    "measure",
    "measure_size",
    "openai_tools",
]
