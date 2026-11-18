"""The build guide must stay consistent: runs scripts/check_guide.py and fails with its report."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_guide_is_consistent() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "check_guide.py"), "--root", str(ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
