"""`_ml_path`: the only place that makes MiniLocker importable, and the advice an operator gets when it is not there."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from toolforge.errors import SandboxUnavailable
from toolforge.sandbox import _ml_path


@pytest.fixture(autouse=True)
def _isolated_import_state() -> Iterator[None]:
    """Each test starts with no MiniLocker imported and nothing cached, and leaves sys.path and sys.modules as it found them.

    Other test files import the real MiniLocker; it is set aside here so `import minilocker` finds the fake backends below.
    """

    def is_ml(name: str) -> bool:
        return name == "minilocker" or name.startswith("minilocker.")

    path = list(sys.path)
    stashed = {k: sys.modules.pop(k) for k in [k for k in sys.modules if is_ml(k)]}
    _ml_path.reset_for_tests()
    yield
    sys.path[:] = path
    for name in [k for k in sys.modules if is_ml(k)]:
        del sys.modules[name]
    sys.modules.update(stashed)
    _ml_path.reset_for_tests()


def fake_backend(root: Path, **modules: str) -> Path:
    """A minimal `backend` directory holding a `minilocker` package; extra keyword arguments are submodules."""
    backend = root / "backend"
    (backend / "minilocker").mkdir(parents=True)
    (backend / "minilocker" / "__init__.py").write_text("MARK = 'fake'\n")
    for name, source in modules.items():
        (backend / "minilocker" / f"{name}.py").write_text(source)
    return backend


def git(*args: str, cwd: Path) -> str:
    env = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    return subprocess.run(
        ["git", *env, *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_missing_submodule_names_the_three_commands_and_the_init_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_ml_path, "repo_root", lambda: tmp_path)  # an empty checkout
    with pytest.raises(SandboxUnavailable) as caught:
        _ml_path.ensure_importable()
    text = str(caught.value) + (caught.value.hint or "")
    assert caught.value.code == "sandbox_unavailable"
    for command in _ml_path.ADD_COMMANDS:
        assert command in text
    assert _ml_path.INIT_COMMAND in text and "toolforge doctor" in text
    assert _ml_path.PINNED_COMMIT in text and "never run on the host" in text


def test_the_three_commands_are_exactly_what_the_patch_file_promises() -> None:
    assert _ml_path.ADD_COMMANDS == (
        "git submodule add https://github.com/shaikh-dotcom/MiniLocker.git third_party/minilocker",
        "git -C third_party/minilocker checkout 0e76295a986292cdb07795d6efc1570f9d0c1acb",
        "git add .gitmodules third_party/minilocker",
    )


def test_an_override_that_is_wrong_says_so_instead_of_blaming_the_submodule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(tmp_path))
    with pytest.raises(SandboxUnavailable) as caught:
        _ml_path.ensure_importable()
    assert "TOOLFORGE_MINILOCKER_PATH" in str(caught.value) and "git submodule add" not in str(caught.value)


def test_the_override_is_used_and_put_on_sys_path_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = fake_backend(tmp_path)
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(backend))
    assert str(backend) not in sys.path
    assert _ml_path.ensure_importable() == backend
    assert _ml_path.ensure_importable() == backend
    assert sys.path.count(str(backend)) == 1
    assert _ml_path.load("minilocker").MARK == "fake"  # type: ignore[attr-defined]


def test_nothing_is_added_to_sys_path_until_something_asks(tmp_path: Path) -> None:
    before = list(sys.path)
    _ml_path.backend_dir()  # looking is free
    assert sys.path == before


def test_load_refuses_names_that_are_not_minilocker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(fake_backend(tmp_path)))
    for name in ("os", "docker", "minilockerx", "toolforge.errors"):
        with pytest.raises(ValueError, match="not a MiniLocker module"):
            _ml_path.load(name)


def test_a_missing_docker_package_is_explained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend = fake_backend(tmp_path, needs_docker="import docker\n")
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(backend))
    monkeypatch.setitem(sys.modules, "docker", None)  # makes `import docker` fail as if it were not installed
    with pytest.raises(SandboxUnavailable) as caught:
        _ml_path.load("minilocker.needs_docker")
    assert ".[sandbox]" in (caught.value.hint or "")


def test_any_other_import_failure_points_at_the_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend = fake_backend(tmp_path, broken="import definitely_not_a_module_xyz\n")
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(backend))
    with pytest.raises(SandboxUnavailable) as caught:
        _ml_path.load("minilocker.broken")
    assert "pinned commit" in (caught.value.hint or "") and "definitely_not_a_module_xyz" in str(caught.value)


def test_minilocker_available_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _ml_path.minilocker_available() in (True, False)
    _ml_path.reset_for_tests()  # the first call caches where MiniLocker is; a new setting needs a reset
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(tmp_path))
    assert _ml_path.minilocker_available() is False


def test_checkout_commit_reads_the_commit_of_the_checkout_that_owns_backend(tmp_path: Path) -> None:
    checkout = tmp_path / "ml"
    backend = fake_backend(checkout)
    backend = checkout / "backend"
    git("init", "-q", cwd=checkout)
    git("add", ".", cwd=checkout)
    git("commit", "-q", "-m", "x", cwd=checkout)
    assert _ml_path.checkout_commit(backend) == git("rev-parse", "HEAD", cwd=checkout)


def test_checkout_commit_does_not_report_an_enclosing_repository_as_minilocker(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    git("init", "-q", cwd=outer)
    (outer / "f").write_text("x")
    git("add", ".", cwd=outer)
    git("commit", "-q", "-m", "x", cwd=outer)
    backend = outer / "third_party" / "ml" / "backend"
    backend.mkdir(parents=True)  # a plain directory inside someone else's repository
    assert _ml_path.checkout_commit(backend) is None


def test_checkout_commit_is_none_without_git_or_a_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _ml_path.checkout_commit(tmp_path / "nothing" / "backend") is None
    monkeypatch.setattr(
        _ml_path.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git"))
    )
    assert _ml_path.checkout_commit(tmp_path) is None


def test_the_real_submodule_is_importable_and_at_the_pin(minilocker: None) -> None:
    backend = _ml_path.ensure_importable()
    assert (backend / "minilocker" / "__init__.py").is_file()
    commit = _ml_path.checkout_commit(backend)
    if commit is None:
        pytest.skip("the MiniLocker checkout is not a git checkout")
    assert commit == _ml_path.PINNED_COMMIT
