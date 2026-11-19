"""Every reference pack under examples/toolpacks validates with no errors and no warnings (and stays that way)."""

from __future__ import annotations

from pathlib import Path

import pytest

from toolforge.config import Settings
from toolforge.models.package import ToolPackage

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"
PACKS = sorted(path.name for path in EXAMPLES.iterdir() if path.is_dir())


def test_the_two_reference_packs_exist() -> None:
    assert {"word_count", "csv_summary"} <= set(PACKS)


@pytest.mark.parametrize("pack", PACKS)
def test_reference_pack_validates_cleanly(pack: str, settings: Settings) -> None:
    package = ToolPackage.from_dir(EXAMPLES / pack)
    report = package.validate(settings)  # real, isolated schema check
    assert report.ok, [f"{i.code} {i.path}: {i.message}" for i in report.issues]
    assert report.warnings == [], [w.code for w in report.warnings]
    assert package.manifest.name == pack


@pytest.mark.parametrize("pack", PACKS)
def test_reference_pack_has_a_stable_hash(pack: str) -> None:
    first = ToolPackage.from_dir(EXAMPLES / pack).code_sha
    assert first == ToolPackage.from_dir(EXAMPLES / pack).code_sha and len(first) == 64


def test_the_packs_cover_both_permission_shapes() -> None:
    word_count = ToolPackage.from_dir(EXAMPLES / "word_count").manifest
    csv_summary = ToolPackage.from_dir(EXAMPLES / "csv_summary").manifest
    assert word_count.permissions.inputs is False and csv_summary.permissions.inputs is True
    for manifest in (word_count, csv_summary):
        assert manifest.permissions.network == [] and manifest.permissions.secrets == []
