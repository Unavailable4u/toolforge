"""The demo's own fixtures: the honest `csv_totals` tool is a valid package whose examples and tests hold, and the hostile ones are what act 3 says they are."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from demo import hostile, tasks
from toolforge.config import Settings
from toolforge.models.package import ToolPackage

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "demo" / "fixtures" / "csv_totals"


def test_csv_totals_is_a_valid_package() -> None:
    report = ToolPackage.from_dir(PACK).validate(Settings())
    assert report.ok, [(i.code, i.message) for i in report.issues]
    manifest = ToolPackage.from_dir(PACK).manifest
    assert manifest.permissions.network == [] and manifest.permissions.secrets == []


def test_csv_totals_examples_give_their_stated_outputs(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("csv_totals_tool", PACK / "tool.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = ToolPackage.from_dir(PACK).manifest
    for example in manifest.examples:
        for name, text in example.files.items():
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        args = {**example.input, "path": str(tmp_path / example.input["path"])}
        assert module.run(args) == example.output


def test_csv_totals_own_tests_pass() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=PACK,
        env={**os.environ, "PYTHONPATH": str(PACK)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-800:]


def test_the_hostile_packages_are_valid_manifests_that_lie_about_nothing_but_intent() -> None:
    for name in hostile.NAMES:
        payload = hostile.payload(name)
        assert payload["manifest"]["name"] == name
        assert payload["manifest"]["permissions"]["network"] == [], (
            "the hostile tool declares no network, which is the lie G1 catches"
        )


def test_the_first_hostile_package_reads_credentials_and_opens_a_connection_to_the_collector() -> None:
    source = hostile.payload(hostile.ATTEMPT_ONE)["files"]["tool.py"]
    assert "socket" in source and hostile.COLLECTOR in source and "credentials" in source


def test_the_second_is_the_catalogues_decoy_reader_with_the_marker() -> None:
    from toolforge.attacks.catalog import DECOY_READ

    assert DECOY_READ in hostile.payload(hostile.ATTEMPT_TWO)["files"]["tool.py"]
    assert "socket" not in hostile.payload(hostile.ATTEMPT_TWO)["files"]["tool.py"]


def test_the_collector_host_can_never_resolve() -> None:
    assert hostile.COLLECTOR.endswith(".invalid")


def test_the_blackjack_task_is_checked_against_the_packs_own_engine() -> None:
    instance = tasks.TASK.generate(7)
    assert instance.expected == tasks.outcome() and instance.expected["player_total"] == 17
    assert tasks.check(instance, dict(instance.expected), {}).ok
    wrong = {**instance.expected, "status": "invented"}
    assert not tasks.check(instance, wrong, {}).ok and not tasks.check(instance, "text", {}).ok
    assert tasks.TASK.id not in {t.id for t in __import__("bench.tasks", fromlist=["TASKS"]).TASKS}


@pytest.mark.parametrize("seed", [1, 7, 99])
def test_the_blackjack_deal_does_not_depend_on_the_demo_seed(seed: int) -> None:
    assert tasks.TASK.generate(seed).expected == tasks.TASK.generate(7).expected
