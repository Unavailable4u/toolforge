"""Shared fixtures and marker handling.

Markers `docker`, `llm`, `attacks` and `browser` are declared in pyproject.toml. Tests carrying them are skipped automatically
when their prerequisite is missing, so `pytest` is always safe to run.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path

import pytest

from toolforge.config import RUNTIME_IMAGE, Settings
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord

# The autouse fixture below removes every TOOLFORGE_* variable so tests never see a developer's settings. A developer who keeps
# MiniLocker somewhere other than the submodule says so in TOOLFORGE_MINILOCKER_PATH; remember it here, before it is removed,
# for the tests that need MiniLocker (the `minilocker` fixture).
_INVOKER_MINILOCKER_PATH = os.environ.get("TOOLFORGE_MINILOCKER_PATH")


@lru_cache(maxsize=1)
def docker_available() -> bool:
    """True when a Docker daemon answers a ping. The `docker` package is imported lazily and may be absent."""
    try:
        import docker  # type: ignore[import-not-found]

        docker.from_env(timeout=3).ping()
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def browser_available() -> bool:
    """True when Playwright and a launchable Chromium exist; the site's browser tests are skipped otherwise."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
    except Exception:
        return False
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "docker" in item.keywords and not docker_available():
            item.add_marker(pytest.mark.skip(reason="no Docker daemon answered a ping"))
        if "attacks" in item.keywords and not docker_available():
            item.add_marker(pytest.mark.skip(reason="the attack suite needs Docker"))
        if "llm" in item.keywords and not os.environ.get("TOOLFORGE_LLM_BASE_URL"):
            item.add_marker(pytest.mark.skip(reason="TOOLFORGE_LLM_BASE_URL is not set"))
        if "browser" in item.keywords and not browser_available():
            item.add_marker(
                pytest.mark.skip(
                    reason="Playwright with Chromium is not installed (pip install playwright; playwright install chromium)"
                )
            )


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the developer's own TOOLFORGE_* variables or .env file."""
    for name in list(os.environ):
        if name.startswith("TOOLFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(Path(__file__).parent)  # a stray .env in the repository root is not read


@pytest.fixture
def minilocker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """For tests that need the real MiniLocker: skip unless it can be imported, through the same lookup production uses."""
    from toolforge.sandbox import _ml_path

    if _INVOKER_MINILOCKER_PATH:
        monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", _INVOKER_MINILOCKER_PATH)
    _ml_path.reset_for_tests()
    if not _ml_path.minilocker_available():
        pytest.skip("MiniLocker is not available (run `git submodule update --init third_party/minilocker`)")
    yield
    _ml_path.reset_for_tests()


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


# --- Registry (P03) --------------------------------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[Registry]:
    """A registry in a throwaway data directory."""
    with Registry(tmp_path / "registry-home") as reg:
        yield reg


@pytest.fixture
def make_package() -> Callable[..., ToolPackage]:
    """Build a package (not validated against settings; the registry only needs a parsable manifest) with changes applied.

    `make_package("pdf_text", "1.2.0", description="...", network=["api.example.com"])`. The code differs per name and
    version, so two packages never share a `code_sha` by accident.
    """

    def build(
        name: str = "word_count",
        version: str = "1.0.0",
        *,
        description: str | None = None,
        when_to_use: str | None = None,
        network: tuple[str, ...] | list[str] = (),
        secrets: tuple[str, ...] | list[str] = (),
        examples: list[dict] | None = None,
        parent_version: str | None = None,
    ) -> ToolPackage:
        manifest = copy.deepcopy(BASE_MANIFEST)
        manifest.update(name=name, version=version, parent_version=parent_version)
        if description is not None:
            manifest["description"] = description
        if when_to_use is not None:
            manifest["when_to_use"] = when_to_use
        if examples is not None:
            manifest["examples"] = examples
        manifest["permissions"]["network"] = list(network)
        manifest["permissions"]["secrets"] = list(secrets)
        files = dict(BASE_FILES)
        files["tool.py"] += f"# {name} {version}\n"
        return ToolPackage(manifest, files)

    return build


@pytest.fixture
def activate(registry: Registry) -> Callable[[VersionRecord], VersionRecord]:
    """Walk a registered version through the pipeline to `verified` and on to `active`, as the pipeline and operator would."""

    def run(record: VersionRecord) -> VersionRecord:
        registry.transition(record.id, "verifying", actor="pipeline")
        registry.transition(record.id, "verified", actor="pipeline")
        registry.transition(record.id, "active", actor="pipeline")
        found = registry.get_version(record.id)
        assert found is not None
        return found

    return run


# Docker tests that run tools use the default runtime image, which is built locally (`toolforge images build`, P14a) and never pulled.
# When it is not built they skip with the command that builds it. These modules choose their own image or build it themselves.
_OWN_IMAGE = {
    "test_minilocker_runner",
    "test_egress",
    "test_server_runtime_probe",
    "test_runtime_image",
    "test_browser_plans",
}


@lru_cache(maxsize=1)
def _runtime_image_built() -> bool:
    try:
        import docker

        client = docker.from_env(timeout=5)
        try:
            client.images.get(RUNTIME_IMAGE)
            return True
        finally:
            client.close()
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _docker_tests_need_the_runtime_image(request: pytest.FixtureRequest) -> None:
    path = Path(str(request.node.path))
    if path.parent.name != "docker" or path.stem in _OWN_IMAGE:
        return
    if not _runtime_image_built():
        pytest.skip(f"the runtime image {RUNTIME_IMAGE} is not built: run `toolforge images build`")
