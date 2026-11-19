"""The one place that makes MiniLocker importable.

MiniLocker has no `pyproject.toml`, so its `backend/` directory is put on `sys.path` here and nowhere else. Every other module
that needs MiniLocker (the allowed importers listed in `tests/test_import_boundary.py`) asks `load("minilocker.<module>")`
for it, so a missing submodule, a missing `docker` package or a broken checkout always surfaces as one `SandboxUnavailable`
with advice an operator can act on, and `import toolforge` itself never needs MiniLocker or Docker.

Nothing from MiniLocker is copied into this repository (docs/guide/03-minilocker-integration.md). Proven by:
`tests/test_import_boundary.py` (who may import it, and that nobody imports it at module level) and `tests/test_ml_path.py`
(the advice, the override, the lazy path insertion).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from toolforge.config import Settings, load_settings
from toolforge.errors import SandboxUnavailable

PINNED_COMMIT = "0e76295a986292cdb07795d6efc1570f9d0c1acb"
SUBMODULE_URL = "https://github.com/shaikh-dotcom/MiniLocker.git"
SUBMODULE_PATH = "third_party/minilocker"

ADD_COMMANDS = (
    f"git submodule add {SUBMODULE_URL} {SUBMODULE_PATH}",
    f"git -C {SUBMODULE_PATH} checkout {PINNED_COMMIT}",
    f"git add .gitmodules {SUBMODULE_PATH}",
)
INIT_COMMAND = f"git submodule update --init {SUBMODULE_PATH}"

_ready: Path | None = None  # the backend directory once it has been verified and put on sys.path


def repo_root() -> Path:
    """The repository root of a source checkout (src/toolforge/sandbox/_ml_path.py is three levels below it)."""
    return Path(__file__).resolve().parents[3]


def default_backend_dir() -> Path:
    return repo_root() / SUBMODULE_PATH / "backend"


def backend_dir(settings: Settings | None = None) -> Path:
    """`TOOLFORGE_MINILOCKER_PATH` (read through Settings, the only reader of the environment) or the submodule."""
    settings = settings if settings is not None else load_settings()
    return settings.minilocker_path if settings.minilocker_path is not None else default_backend_dir()


def advice(backend: Path, overridden: bool) -> str:
    """What an operator should do about a missing MiniLocker. Names the three commands from the P04 patch file."""
    if overridden:
        return (
            f"TOOLFORGE_MINILOCKER_PATH is {backend}, but there is no `minilocker` package in it. Point it at the "
            f"`backend` directory of a MiniLocker checkout (the one that contains the `minilocker` folder), "
            f"or unset it to use the {SUBMODULE_PATH} submodule."
        )
    commands = "\n".join(f"  {cmd}" for cmd in ADD_COMMANDS)
    return (
        f"MiniLocker is missing: {SUBMODULE_PATH} is empty or absent, so there is no sandbox to run tool code in "
        f"(tool code is never run on the host).\n"
        f"If .gitmodules already lists it (after applying patch P04), fetch it with:\n  {INIT_COMMAND}\n"
        f"Otherwise add the pinned submodule with these three commands:\n{commands}"
    )


def ensure_importable(settings: Settings | None = None) -> Path:
    """Put MiniLocker's `backend/` on `sys.path` (once) and return it; raise `SandboxUnavailable` when it is not there."""
    global _ready
    if _ready is not None:
        return _ready
    resolved = settings if settings is not None else load_settings()
    backend = backend_dir(resolved)
    if not (backend / "minilocker" / "__init__.py").is_file():
        raise SandboxUnavailable(
            advice(backend, overridden=resolved.minilocker_path is not None),
            hint="then run `toolforge doctor`",
        )
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    _ready = backend
    return backend


def load(name: str, settings: Settings | None = None) -> ModuleType:
    """Import `minilocker` or one of its modules, for example `load("minilocker.sandbox.snapshot")`."""
    if name != "minilocker" and not name.startswith("minilocker."):
        raise ValueError(f"not a MiniLocker module: {name}")
    ensure_importable(settings)
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        raise SandboxUnavailable(
            f"could not import {name}: {type(exc).__name__}: {str(exc)[:200]}",
            hint=(
                'MiniLocker needs the Python package "docker": install it with `pip install -e ".[sandbox]"`'
                if str(missing).split(".")[0] in {"docker", "requests"}
                else "check that the submodule is at the pinned commit (`toolforge doctor`)"
            ),
        ) from exc


def minilocker_available(settings: Settings | None = None) -> bool:
    """True when MiniLocker can be imported. Never raises; used to pick the pure-Python fallbacks and to skip tests."""
    try:
        load("minilocker", settings)
    except SandboxUnavailable:
        return False
    return True


def checkout_commit(backend: Path) -> str | None:
    """The commit of the MiniLocker checkout whose `backend/` is `backend`, or None when that is not a git checkout.

    The checkout's top level must be the parent of `backend`: a plain directory inside some other repository (this one,
    say) must not report that repository's commit as MiniLocker's.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(backend), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.stdout.split("\n")
    if out.returncode != 0 or len(lines) < 2:
        return None
    top, commit = lines[0].strip(), lines[1].strip()
    if len(commit) != 40 or Path(top).resolve() != backend.resolve().parent:
        return None
    return commit


def reset_for_tests() -> None:
    """Forget the cached location so a test can point `TOOLFORGE_MINILOCKER_PATH` somewhere else."""
    global _ready
    _ready = None
