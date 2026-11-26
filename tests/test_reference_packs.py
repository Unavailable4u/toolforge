"""The reference packs of P14a and P14b pass gates G1 to G4 and their own tests, against the REAL harness through a stand-in sandbox.
A browser plan (P14b) has no code: its gates run in this process against its recorded pages.

The stand-in is not isolated and runs the host's Python (trusted test code only), so a pack whose library is not installed here
(`chess`, `gymnasium`) is skipped; the image has them, and `tests/docker/test_golden_packs.py` runs every pack inside it.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from local_sandbox import LocalRunner

from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle
from toolforge.verify.pipeline import Pipeline

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"
NEEDS = {"chess_session": "chess", "frozenlake_session": "gymnasium"}
NEW_PACKS = [
    "sqlite_query",
    "fixed_width_parse",
    "tlv_decode",
    "http_json_fetch",
    "chess_session",
    "blackjack_session",
    "frozenlake_session",
    "browser_headlines",
]
NETWORK_PACKS = {"http_json_fetch": ["api.github.com"], "browser_headlines": ["headlines.example.org"]}
PLAN_PACKS = {"browser_headlines"}
SESSION_PACKS = {"chess_session", "blackjack_session", "frozenlake_session"}


@pytest.fixture
def runner():
    local = LocalRunner()
    yield local
    local.cleanup()


@pytest.mark.parametrize("pack", NEW_PACKS)
def test_the_pack_passes_the_gates_that_can_run_without_a_container(
    pack: str, runner, tmp_path: Path
) -> None:
    module = NEEDS.get(pack)
    if module and importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} is not installed here")
    settings = Settings(home=tmp_path / "home")
    package = ToolPackage.from_dir(EXAMPLES / pack)
    with Registry(settings.home) as registry:
        report = Pipeline(settings, runner, registry, ledger_factory=ToolforgeLedger, fuzz_inputs=8).run(
            package, "test"
        )
        gates = {gate.id: gate for gate in report.gates}
        assert report.registered, report.text()
        for name in ("G1", "G3", "G4"):
            assert gates[name].status == "passed", f"{name}: {gates[name].summary}"
        assert report.status in (lifecycle.VERIFIED_BASIC, lifecycle.PENDING_APPROVAL), report.text()


def test_the_new_packs_have_the_kinds_and_permissions_the_guide_describes() -> None:
    for pack in NEW_PACKS:
        manifest = ToolPackage.from_dir(EXAMPLES / pack).manifest
        kind = "browser_plan" if pack in PLAN_PACKS else "session" if pack in SESSION_PACKS else "stateless"
        assert manifest.kind == kind, pack
        assert manifest.permissions.browser is (pack in PLAN_PACKS), pack
        assert manifest.permissions.network == NETWORK_PACKS.get(pack, []), pack
        assert manifest.permissions.secrets == [] and manifest.requirements == [], pack
        assert manifest.permissions.inputs is (pack == "sqlite_query"), pack
