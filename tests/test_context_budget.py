"""The context budget (P12): what the server sends a model must not grow with the registry, and must fit the constants in `server/budget.py`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from server_helpers import Env, data, make_env

from toolforge.server import budget
from toolforge.server.budget import BUDGET, estimate_tokens, fit_describe, size_of


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = make_env(tmp_path)
    yield environment
    environment.registry.close()


def tools_list_payload(env: Env) -> str:
    listed = env.session(lambda client: client.list_tools()).tools
    return json.dumps(
        [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in listed], sort_keys=True
    )


def add_tools(env: Env, start: int, stop: int) -> None:
    for n in range(start, stop):
        env.add_tool(
            name=f"text_tool_{n:03d}",
            description=f"Count the words in a piece of text number {n} and report how many words there are in it.",
            when_to_use="The task gives some text and asks how many words it has.",
        )


def test_the_tools_list_does_not_change_with_the_registry_and_fits_its_budget(env: Env) -> None:
    add_tools(env, 0, 10)
    small = tools_list_payload(env)
    add_tools(env, 10, 60)
    large = tools_list_payload(env)
    assert small == large
    assert len(large) <= BUDGET.tools_list_chars, f"tools/list is {len(large)} characters"


def test_a_find_response_is_bounded_whatever_the_registry_holds(env: Env) -> None:
    add_tools(env, 0, 10)
    small = data(env.call("forge_find", {"query": "count words text report"}))
    add_tools(env, 10, 60)
    large = data(env.call("forge_find", {"query": "count words text report"}))
    assert small["count"] == large["count"] == 5
    for response in (small, large):
        assert size_of(response) <= BUDGET.find_chars
    assert size_of(large) < size_of(small) * 1.2  # a bigger registry does not mean a bigger answer


def test_a_describe_response_with_its_signature_fits_its_budget(env: Env) -> None:
    ref = env.add_tool(
        name="word_counter", description="Count the words in a piece of text and report how many there are."
    )
    response = data(env.call("forge_describe", {"name": ref.split("@")[0]}))
    assert response["signature"]["python"] and size_of(response) <= BUDGET.describe_chars
    assert not response.get("truncated")


def test_a_response_that_is_too_big_is_cut_down_in_a_fixed_order_and_says_so() -> None:
    response = {
        "name": "t",
        "signature": {"python": "def t(): ..."},
        "manifest": {
            "examples": [{"input": {"x": "y" * 3000}} for _ in range(5)],
            "output_schema": {"a": "b" * 3000},
        },
        "lineage": {"versions": [{"version": f"1.0.{n}"} for n in range(20)]},
        "gates": [{"hints": ["h" * 500] * 6} for _ in range(8)],
        "blast_radius": {"status": "measured", "detail": ["d" * 500] * 50},
    }
    fitted = fit_describe(response, limit=9000)
    assert fitted["truncated"] is True and "9000" in fitted["budget_note"]
    assert size_of(fitted) <= 9000
    assert fitted["signature"] == {"python": "def t(): ..."} and len(fitted["lineage"]["versions"]) == 5
    untouched = {
        "name": "t",
        "manifest": {},
        "lineage": {"versions": []},
        "gates": [],
        "blast_radius": {"status": "x"},
    }
    assert "truncated" not in fit_describe(dict(untouched))


def test_a_huge_manifest_cannot_fill_the_context(env: Env) -> None:
    text = " ".join(["w"] * 400)
    examples = [
        {"input": {"text": text}, "output": {"text": text, "words": 400}, "live": False} for _ in range(5)
    ]
    ref = env.add_tool(name="word_counter", examples=examples)
    response = data(env.call("forge_describe", {"name": ref.split("@")[0]}))
    assert size_of(response) <= BUDGET.describe_chars


def test_token_figures_are_estimates() -> None:
    assert estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2 and estimate_tokens("") == 0
    assert budget.BASIS == "estimated"
