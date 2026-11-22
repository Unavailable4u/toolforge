"""One optional test against a real OpenAI-compatible endpoint. It checks structure only, never what the model says.

Run it with `TOOLFORGE_LLM_BASE_URL`, `TOOLFORGE_LLM_MODEL` (and `TOOLFORGE_LLM_API_KEY` if the server wants one) set:
`pytest -m llm`. It sends two short sentences that hold no secret and no tool code. Without the variables it is skipped.
"""

from __future__ import annotations

import os

import pytest

from toolforge.config import Settings
from toolforge.llm.client import client_from_settings

# conftest removes TOOLFORGE_* from the environment before every test, so read it once, when the file is collected
LIVE = {k: v for k, v in os.environ.items() if k.startswith("TOOLFORGE_LLM_")}


@pytest.mark.llm
def test_a_real_endpoint_returns_one_json_object_and_reports_its_cost() -> None:
    settings = Settings(
        llm_base_url=LIVE.get("TOOLFORGE_LLM_BASE_URL"),
        llm_model=LIVE.get("TOOLFORGE_LLM_MODEL"),
        llm_api_key=LIVE.get("TOOLFORGE_LLM_API_KEY"),
    )
    client = client_from_settings(settings, "builder")
    assert client is not None, "set TOOLFORGE_LLM_BASE_URL and TOOLFORGE_LLM_MODEL"
    reply = client.complete_json(
        "You answer with a JSON object.",
        'Return an object with one key, "ok", whose value is true.',
        schema_hint='{"ok": boolean}',
    )
    assert isinstance(reply.data, dict) and reply.requests in (1, 2)
    assert isinstance(reply.model, str) and reply.model
    assert reply.latency_ms >= 0
    assert reply.prompt_tokens is None or reply.prompt_tokens >= 0
