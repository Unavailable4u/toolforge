"""Shared fixtures and marker handling.

Markers `docker`, `llm` and `attacks` are declared in pyproject.toml. Tests carrying them are skipped automatically
when their prerequisite is missing, so `pytest` is always safe to run. (P19 adds the `browser` marker.)
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path

import pytest

from toolforge.config import Settings


@lru_cache(maxsize=1)
def docker_available() -> bool:
    """True when a Docker daemon answers a ping. The `docker` package is imported lazily and may be absent."""
    try:
        import docker  # type: ignore[import-not-found]

        docker.from_env(timeout=3).ping()
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "docker" in item.keywords and not docker_available():
            item.add_marker(pytest.mark.skip(reason="no Docker daemon answered a ping"))
        if "attacks" in item.keywords and not docker_available():
            item.add_marker(pytest.mark.skip(reason="the attack suite needs Docker"))
        if "llm" in item.keywords and not os.environ.get("TOOLFORGE_LLM_BASE_URL"):
            item.add_marker(pytest.mark.skip(reason="TOOLFORGE_LLM_BASE_URL is not set"))


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the developer's own TOOLFORGE_* variables or .env file."""
    for name in list(os.environ):
        if name.startswith("TOOLFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(Path(__file__).parent)  # a stray .env in the repository root is not read


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throwaway data directory, wired in through TOOLFORGE_HOME."""
    home = tmp_path / "toolforge-home"
    home.mkdir()
    monkeypatch.setenv("TOOLFORGE_HOME", str(home))
    yield home


# --- Tool packages (P02) ---------------------------------------------------------------------------------------------

BASE_MANIFEST: dict = {
    "name": "word_count",
    "version": "1.0.0",
    "parent_version": None,
    "description": "Count the words in a piece of text and report how many there are.",
    "when_to_use": "The task gives some text and asks how many words it has.",
    "entrypoint": "tool.py:run",
    "kind": "stateless",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "output_schema": {"type": "object", "properties": {"words": {"type": "integer"}}, "required": ["words"]},
    "permissions": {"network": [], "secrets": [], "inputs": False, "browser": False},
    "limits": {"timeout_s": 10, "memory_mb": 128, "output_kb": 16},
    "requirements": [],
    "examples": [{"input": {"text": "a b"}, "output": {"words": 2}, "live": False}],
}
BASE_FILES: dict[str, str] = {
    "tool.py": "def run(args):\n    return {'words': len(args['text'].split())}\n",
    "tests/test_tool.py": (
        "from tool import run\n\n"
        "def test_two():\n    assert run({'text': 'a b'}) == {'words': 2}\n\n"
        "def test_none():\n    assert run({'text': ''}) == {'words': 0}\n\n"
        "def test_spaces():\n    assert run({'text': ' a  b  c '}) == {'words': 3}\n"
    ),
}


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def make_payload() -> Callable[[], dict]:
    """A fresh, valid submission payload `{"manifest": ..., "files": ...}` that a test may mutate freely."""
    return lambda: {"manifest": copy.deepcopy(BASE_MANIFEST), "files": dict(BASE_FILES)}


@pytest.fixture
def inline_schema_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check examples against schemas in-process. Faster; the isolation tests and the CLI keep the real subprocess."""
    from toolforge.models import package

    monkeypatch.setattr(package, "SCHEMA_CHECK_ISOLATED", False)
