"""Repository hygiene for a git checkout: LF only, no patches or .env committed, no secret-shaped example values."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BINARY_SUFFIXES = {".png", ".gz", ".tfbundle", ".zip", ".ico", ".jpg", ".jpeg", ".woff", ".woff2"}
SECRET_SHAPES = [
    r"AKIA[0-9A-Z]{16}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"AIza[0-9A-Za-z_-]{35}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)(key|token|secret|password)\s*=\s*[\"']?[A-Za-z0-9+/_-]{20,}",
]


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    return [line for line in out.splitlines() if line]


def test_no_carriage_returns_in_tracked_text_files() -> None:
    bad = [
        name
        for name in _tracked()
        if Path(name).suffix not in BINARY_SUFFIXES
        and (ROOT / name).is_file()
        and b"\r" in (ROOT / name).read_bytes()
    ]
    assert not bad, f"CRLF found (use LF): {bad}"


def test_no_patch_or_env_files_tracked() -> None:
    bad = [
        n
        for n in _tracked()
        if n.endswith(".patch")
        or Path(n).name == ".env"
        or Path(n).name.startswith(".env.")
        and n != ".env.example"
    ]
    assert not bad, f"must not be committed: {bad}"


def test_env_example_has_no_secret_shaped_values() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    uncommented = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for pattern in SECRET_SHAPES:
        assert not re.search(pattern, uncommented), f"secret-shaped value in .env.example ({pattern})"
        for line in text.splitlines():
            if line.lstrip().startswith("# TOOLFORGE_") and "=" in line:
                assert not re.search(pattern, line.split("=", 1)[1]), (
                    f"secret-shaped example value ({pattern})"
                )
