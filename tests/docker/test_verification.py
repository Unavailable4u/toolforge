"""Gates G1 to G4 against a real Docker daemon: `Pipeline` -> `MiniLockerRunner` -> `harness.py` in a container.

Skipped automatically when no daemon answers (see tests/conftest.py). They show what the stand-in tests cannot: that the reference packs
verify inside the real runtime image, that a package whose tests check nothing is rejected there, that a crash is found by fuzzing
there, and that the build step (the only one with network access) refuses a requirement that has no wheel.

The build test needs the network hosts in `build_allow` (the default is the package index hosts). It is the one test here that depends
on the outside world.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from toolforge.cli_run import make_ledger
from toolforge.config import Settings
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle
from toolforge.sandbox.minilocker_runner import MiniLockerRunner
from toolforge.services.forge import Forge
from toolforge.verify.pipeline import Pipeline

pytestmark = [pytest.mark.docker, pytest.mark.usefixtures("minilocker")]

PACKS = Path(__file__).resolve().parent.parent.parent / "examples" / "toolpacks"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def sandbox(settings: Settings) -> Iterator[MiniLockerRunner]:
    runner = MiniLockerRunner(settings=settings)
    yield runner
    runner.shutdown()


def package_with(**changes) -> ToolPackage:
    from verify_helpers import valid_package

    return valid_package(**changes)


@pytest.mark.parametrize("pack", ["word_count", "csv_summary"])
def test_a_reference_pack_passes_g1_to_g4_and_g6_and_ends_verified_basic_with_g5_skipped(
    pack, settings, sandbox
) -> None:
    report = Pipeline(settings, sandbox, None).run(ToolPackage.from_dir(PACKS / pack), "test")
    assert [(g.id, g.status) for g in report.gates] == [
        ("G1", "passed"), ("G2", "passed"), ("G3", "passed"), ("G4", "passed"), ("G5", "skipped"), ("G6", "passed"),
    ], report.text()  # fmt: skip
    assert report.status == lifecycle.VERIFIED_BASIC


def test_a_submission_is_recorded_in_the_registry(settings, sandbox) -> None:
    with Registry.from_settings(settings) as registry:
        forge = Forge(settings, registry, sandbox, ledger_factory=make_ledger)
        report = forge.submit_package(ToolPackage.from_dir(PACKS / "csv_summary"), "test")
        assert report.status == lifecycle.VERIFIED_BASIC
        record = registry.find_version("csv_summary", report.version)
        assert record.blob_sha and registry.blobs.exists(record.blob_sha)


def test_tests_that_check_nothing_fail_g3(settings, sandbox) -> None:
    tests = "from tool import run\n\n\ndef test_a():\n    assert True\n\n\ndef test_b():\n    run({'text': 'x'})\n\n\ndef test_c():\n    assert 1 == 1\n"
    report = Pipeline(settings, sandbox, None).run(package_with(tests=tests), "test")
    g3 = next(g for g in report.gates if g.id == "G3")
    assert (
        report.status == "rejected" and g3.status == "failed" and "do not constrain behaviour" in g3.summary
    )


def test_a_crash_on_an_empty_list_fails_g4(settings, sandbox) -> None:
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "integer"}, "maxItems": 5}},
        "required": ["items"],
        "additionalProperties": False,
    }
    tool = "def run(args):\n    items = args['items']\n    return {'mean': sum(items) / len(items)}\n"
    tests = (
        "from tool import run\n\n\n"
        "def test_mean():\n    assert run({'items': [2, 4]}) == {'mean': 3.0}\n\n\n"
        "def test_single():\n    assert run({'items': [5]}) == {'mean': 5.0}\n\n\n"
        "def test_negative():\n    assert run({'items': [-1, 1]}) == {'mean': 0.0}\n"
    )
    package = package_with(
        tool=tool,
        tests=tests,
        input_schema=schema,
        output_schema={"type": "object"},
        examples=[{"input": {"items": [1, 2, 3]}, "output": {"mean": 2.0}}],
    )
    report = Pipeline(settings, sandbox, None).run(package, "test")
    g4 = next(g for g in report.gates if g.id == "G4")
    assert report.status == "rejected" and g4.status == "failed" and "ZeroDivisionError" in g4.hints[0]


def test_a_requirement_with_no_wheel_fails_g2(settings, sandbox) -> None:
    # a version that does not exist has no wheel; the hint says so. Needs the package index hosts in `build_allow`.
    package = package_with(requirements=["toolforge-no-such-package==0.0.1"])
    report = Pipeline(settings, sandbox, None).run(package, "test")
    g2 = next(g for g in report.gates if g.id == "G2")
    assert report.status == "rejected" and g2.status == "failed" and g2.hints
