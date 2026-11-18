"""Shared fixtures and marker handling.

Markers `docker`, `llm` and `attacks` are declared in pyproject.toml. Tests carrying them are skipped automatically
when their prerequisite is missing, so `pytest` is always safe to run. (P19 adds the `browser` marker.)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

import pytest


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
