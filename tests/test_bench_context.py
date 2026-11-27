"""Context cost (P16): the synthetic registry, the in-memory measurement, and how estimates and measurements are labelled.

No LLM and no Docker: the server runs in memory, runs are answered by a stand-in, and the "endpoint" is a fake transport.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import context, report, synthetic
from toolforge.llm.client import HttpResponse
from toolforge.server.budget import BUDGET, estimate_tokens


@pytest.fixture(scope="module")
def measured() -> dict:
    return context.measure([10, 1000], 1)


def test_estimate_tokens_on_known_strings() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 4000) == 1000


def test_the_same_seed_gives_byte_identical_packages_and_another_seed_does_not() -> None:
    assert synthetic.digest(synthetic.tools(50, 3)) == synthetic.digest(synthetic.tools(50, 3))
    assert synthetic.digest(synthetic.tools(50, 3)) != synthetic.digest(synthetic.tools(50, 4))
    assert synthetic.digest(synthetic.tools(10, 3)) == synthetic.digest(synthetic.tools(50, 3)[:10])


def test_synthetic_packages_are_valid_and_registered_active(tmp_path: Path) -> None:
    from toolforge.config import Settings

    items = synthetic.tools(12, 1)
    for item in items[: len(synthetic.TEMPLATES)]:
        report_ = item.package.validate(Settings(home=tmp_path / "v"))
        assert report_.ok, report_.issues
    registry, items = synthetic.build(tmp_path / "home", 12, 1)
    try:
        versions = registry.list_versions()
        assert len(versions) == 12 and {v.status for v in versions} == {"active"}
        gates = {g.gate: g.status for g in registry.gate_results(versions[0].id)}
        assert gates == {
            "G1": "passed",
            "G2": "passed",
            "G3": "passed",
            "G4": "passed",
            "G5": "skipped",
            "G6": "skipped",
        }
        assert versions[0].blob_sha and registry.blobs.exists(versions[0].blob_sha)
    finally:
        registry.close()
    with pytest.raises(FileExistsError):
        synthetic.build(tmp_path / "home", 1, 1)


def test_the_same_seed_gives_identical_measurements(measured: dict) -> None:
    again = context.measure([10, 1000], 1)
    assert json.dumps(again, sort_keys=True) == json.dumps(measured, sort_keys=True)


def test_meta_tools_list_is_the_same_for_10_and_1000_tools(measured: dict) -> None:
    small, large = measured["results"]
    assert small["meta"]["tools_list"] == large["meta"]["tools_list"]
    assert small["meta"]["tools_listed"] == large["meta"]["tools_listed"]
    assert measured["hypotheses"]["meta_tools_list_constant"] is True


def test_forge_find_stays_inside_its_budget_and_describe_is_measured(measured: dict) -> None:
    for result in measured["results"]:
        meta = result["meta"]
        assert (
            meta["find_results"] >= 1
            and meta["find"]["chars"] <= BUDGET.find_chars
            and meta["find_within_budget"]
        )
        assert meta["describe"] is not None and meta["describe"]["chars"] <= BUDGET.describe_chars
        assert meta["top_hit"]
        parts = meta["tools_list"]["bytes"] + meta["find"]["bytes"] + meta["describe"]["bytes"]
        assert meta["total"]["bytes"] == parts


def test_direct_grows_with_size(measured: dict) -> None:
    small, large = measured["results"]
    assert large["direct"]["tools_list"]["bytes"] > 10 * small["direct"]["tools_list"]["bytes"]
    assert large["direct"]["tools_listed"] - small["direct"]["tools_listed"] == 990
    assert measured["hypotheses"]["direct_grows_with_size"] is True


def test_estimates_are_labelled_estimate(measured: dict) -> None:
    assert measured["method"] == "estimate" and measured["model"] is None
    figure = measured["results"][0]["meta"]["tools_list"]
    assert figure["tokens"] == -(-figure["chars"] // 4)
    text = "\n".join(report.context_markdown(measured))
    assert "**estimate**" in text and "measured**" not in text
    assert "say nothing about any other system" in text


def test_chains_are_measured_only_when_forge_chain_exists(measured: dict) -> None:
    for result in measured["results"]:
        if context.chain_available():
            chain = result["chain"]
            assert chain["steps"] == 3 and chain["separate_runs"]["calls"] == 3
            assert chain["chain"]["total"]["bytes"] > 0 and chain["separate_runs"]["total"]["bytes"] > 0
        else:
            assert result["chain"] is None


class FakeEndpoint:
    """An OpenAI-compatible endpoint that counts one token per 4 characters of the request body, and reports it as usage."""

    def __init__(self, status: int = 200, usage: bool = True) -> None:
        self.status, self.usage = status, usage
        self.bodies: list[dict] = []

    def post(self, url: str, headers: dict, body: bytes, timeout_s: float) -> HttpResponse:
        request = json.loads(body)
        self.bodies.append(request)
        payload = {"choices": [{"message": {"content": "ok"}}], "model": "fake-model"}
        if self.usage:
            payload["usage"] = {"prompt_tokens": len(body) // 4, "completion_tokens": 1}
        return HttpResponse(self.status, {}, json.dumps(payload).encode())


def test_an_endpoint_measurement_is_labelled_measured_and_sends_max_tokens_one() -> None:
    endpoint = FakeEndpoint()
    counter = context.EndpointCounter("http://fake.test/v1", "fake-model", transport=endpoint)
    block = context.measure([10], 1, counter)
    assert block["method"] == "measured" and block["model"] == "fake-model"
    figure = block["results"][0]["meta"]["tools_list"]
    assert isinstance(figure["tokens"], int) and figure["tokens"] > 0
    assert all(body["max_tokens"] == 1 for body in endpoint.bodies)
    with_tools = [body for body in endpoint.bodies if "tools" in body]
    assert with_tools and with_tools[0]["tools"][0]["type"] == "function"
    text = "\n".join(report.context_markdown(block))
    assert "**measured**" in text and "fake-model" in text and "(estimate)" not in text


@pytest.mark.parametrize("endpoint", [FakeEndpoint(status=500), FakeEndpoint(usage=False)])
def test_a_payload_the_endpoint_cannot_count_is_null_never_zero(endpoint: FakeEndpoint) -> None:
    counter = context.EndpointCounter("http://fake.test/v1", "fake-model", transport=endpoint)
    block = context.measure([10], 1, counter)
    meta = block["results"][0]["meta"]
    assert meta["tools_list"]["tokens"] is None and meta["find"]["tokens"] is None
    assert meta["total"]["tokens"] is None and meta["tools_list"]["bytes"] > 0
    assert "not measured" in "\n".join(report.context_markdown(block))


def test_a_missing_context_measurement_is_null_in_summary_json(tmp_path: Path) -> None:
    summary = report.summarise([])
    assert summary["context"] is None
    report.write(tmp_path, summary)
    written = json.loads((tmp_path / "summary.json").read_text())
    assert written["context"] is None and written["attack_lab"]["containment_rate"] is None
    assert "Not measured: `python -m bench context` was not run" in (tmp_path / "report.md").read_text()


def test_the_context_command_writes_context_json_and_a_report(tmp_path: Path) -> None:
    from bench.__main__ import main

    assert main(["context", "--sizes", "5,20", "--seed", "2", "--out", str(tmp_path)]) == 0
    written = json.loads((tmp_path / "context.json").read_text())
    assert written["sizes"] == [5, 20] and written["method"] == "estimate"
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["context"] == written and summary["configs"] == {}


def test_bad_sizes_are_refused() -> None:
    with pytest.raises(ValueError):
        context.measure([], 1)
    with pytest.raises(ValueError):
        context.measure([0], 1)
    with pytest.raises(ValueError):
        context.EndpointCounter("file:///etc/passwd", "m")
