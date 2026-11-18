"""Documented settings and real settings must be the same set (the `.env.example` side of the drift habit)."""

from __future__ import annotations

import re
from pathlib import Path

from toolforge.config import ENV_PREFIX, Settings

ROOT = Path(__file__).resolve().parent.parent


def _documented() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(rf"^#?\s*({ENV_PREFIX}[A-Z0-9_]+)=", text, re.MULTILINE))


def test_every_setting_is_documented() -> None:
    expected = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}
    missing = expected - _documented()
    assert not missing, f".env.example lacks: {sorted(missing)}"


def test_env_example_names_only_real_settings() -> None:
    expected = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}
    unknown = _documented() - expected
    assert not unknown, f".env.example mentions settings that do not exist: {sorted(unknown)}"
