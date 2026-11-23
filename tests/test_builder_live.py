"""One optional end-to-end build against a real OpenAI-compatible endpoint and a real Docker daemon. It checks structure only, never what the
model writes: a build that ends with a package the pipeline did not reject.

Run it with `TOOLFORGE_LLM_BASE_URL`, `TOOLFORGE_LLM_MODEL` (and `TOOLFORGE_LLM_API_KEY` if the server wants one) set and Docker running:
`pytest -m llm tests/test_builder_live.py`. The request holds no secret and no data. Without the variables, or without Docker, it is skipped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from toolforge.cli import main
from toolforge.registry import Registry

# conftest removes TOOLFORGE_* from the environment before every test, so read it once, when the file is collected
LIVE = {k: v for k, v in os.environ.items() if k.startswith("TOOLFORGE_LLM_")}


@pytest.mark.llm
@pytest.mark.docker
def test_a_real_model_builds_a_trivial_tool_that_the_gates_do_not_reject(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if not LIVE.get("TOOLFORGE_LLM_BASE_URL") or not LIVE.get("TOOLFORGE_LLM_MODEL"):
        pytest.skip("set TOOLFORGE_LLM_BASE_URL and TOOLFORGE_LLM_MODEL")
    for key, value in LIVE.items():
        monkeypatch.setenv(key, value)
    code = main(
        [
            "build",
            "count the words in a text and return the number of words",
            "--examples",
            str(_examples(tmp_home)),
            "--json",
            "--quiet",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["ok"] is True, result["summary"]
    assert result["status"] in ("verified_basic", "verified", "pending_approval")
    assert 1 <= result["rounds"] <= result["max_rounds"] and result["usage"]["requests"] >= 1
    with Registry(tmp_home) as registry:
        record = registry.find_version(result["tool"], result["version"])
        assert record is not None and record.created_by == "builder"
        assert registry.active_version(result["tool"]) is None  # the operator promotes; the builder cannot


def _examples(folder: Path) -> Path:
    path = folder / "examples.json"
    path.write_text(
        json.dumps([{"input": {"text": "one two three"}, "output": {"words": 3}}]), encoding="utf-8"
    )
    return path
