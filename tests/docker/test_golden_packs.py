"""The P14a and P14b reference packs against a real Docker daemon and the real runtime image (the 'golden' run).

Each pack goes through gates G1 to G4 and G6 in the image, offline, exactly as a submitted tool would. This is what the stand-in tests
(`tests/test_reference_packs.py`) cannot show: that the libraries the packs import (`chess`, `gymnasium`, `sqlite3`) are in the image at
the pinned versions, and that a session pack replays the same inside a container after a restart of its sandbox.

A browser plan (`browser_headlines`) runs no code in that image. Its G3 and G4 run in this process against its recorded pages, and G6
starts MiniLocker's browser container; the pack is skipped when the browser image is not built (`docs/browser-plans.md`). The pack's
host does not exist, so nothing here opens a page.

Skipped when no daemon answers or the image is not built (`toolforge images build`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from toolforge.config import Settings
from toolforge.models.package import ToolPackage
from toolforge.registry import lifecycle
from toolforge.sandbox.browser_adapter import BrowserAdapter
from toolforge.sandbox.minilocker_runner import MiniLockerRunner
from toolforge.verify.pipeline import Pipeline

pytestmark = [pytest.mark.docker, pytest.mark.usefixtures("minilocker")]

PACKS = Path(__file__).resolve().parent.parent.parent / "examples" / "toolpacks"
GOLDEN = [
    "sqlite_query",
    "fixed_width_parse",
    "tlv_decode",
    "http_json_fetch",
    "chess_session",
    "blackjack_session",
    "frozenlake_session",
    "browser_headlines",
]
NEEDS_APPROVAL = {"http_json_fetch", "browser_headlines"}  # they declare a network host
PLAN_PACKS = {"browser_headlines"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def sandbox(settings: Settings) -> Iterator[MiniLockerRunner]:
    runner = MiniLockerRunner(settings=settings)
    yield runner
    runner.shutdown()


@pytest.mark.parametrize("pack", GOLDEN)
def test_a_golden_pack_passes_the_gates_in_the_real_image(
    pack: str, settings: Settings, sandbox: MiniLockerRunner
) -> None:
    if pack in PLAN_PACKS:
        present, why = BrowserAdapter(settings).available()
        if not present:
            pytest.skip(why)
    report = Pipeline(settings, sandbox, None).run(ToolPackage.from_dir(PACKS / pack), "test")
    statuses = {gate.id: gate.status for gate in report.gates}
    assert [statuses[g] for g in ("G1", "G2", "G3", "G4", "G6")] == ["passed"] * 5, report.text()
    assert statuses["G5"] == "skipped", report.text()
    allowed = (
        (lifecycle.VERIFIED_BASIC, lifecycle.PENDING_APPROVAL)
        if pack in NEEDS_APPROVAL
        else (lifecycle.VERIFIED_BASIC,)
    )
    assert report.status in allowed, report.text()
