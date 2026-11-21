"""Gates G1 to G4 end to end against the REAL in-sandbox harness, through a stand-in sandbox that is not isolated.

Only code that is checked in (the reference packs) or written by this file runs here. It is trusted test code; a stand-in like this must
never be pointed at a submitted package. What the real containers add (isolation, the network rule, limits) is covered by
`tests/docker/test_verification.py`, which needs Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from local_sandbox import LocalRunner
from verify_helpers import MemoryLedgers, valid_package

from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle
from toolforge.services.forge import Forge
from toolforge.verify.pipeline import Pipeline

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"


@pytest.fixture
def runner():
    local = LocalRunner()
    yield local
    local.cleanup()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def verify(package: ToolPackage, runner: LocalRunner, settings: Settings, *, fuzz_inputs: int = 30):
    return Pipeline(settings, runner, None, fuzz_inputs=fuzz_inputs).run(package, "test")


def statuses(report) -> dict[str, str]:
    return {g.id: g.status for g in report.gates}


# --- The reference packs ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("pack", ["word_count", "csv_summary"])
def test_a_reference_pack_passes_g1_to_g4_and_ends_verified_basic(pack: str, runner, settings) -> None:
    report = verify(ToolPackage.from_dir(EXAMPLES / pack), runner, settings)
    assert statuses(report) == {
        "G1": "passed", "G2": "passed", "G3": "passed", "G4": "passed", "G5": "skipped", "G6": "skipped",
    }, report.text()  # fmt: skip
    assert report.status == lifecycle.VERIFIED_BASIC and report.ok
    assert report.dry_run and not report.registered


def test_the_gates_use_one_sandbox_each_with_the_expected_network_rule(runner, settings) -> None:
    verify(ToolPackage.from_dir(EXAMPLES / "word_count"), runner, settings)
    assert [s.label.rsplit(":", 1)[1] for s in runner.specs] == [
        "G3",
        "G4",
        "G6",
    ]  # G2 needs none without requirements
    assert all(s.network == "none" for s in runner.specs)
    assert {s.label.rsplit(":", 1)[1]: s.decoys for s in runner.specs} == {
        "G3": False,
        "G4": False,
        "G6": True,
    }
    assert all(h.closed for h in runner.handles)


# --- Bad packages are rejected at the right gate -----------------------------------------------------------------------------------


def test_g1_rejects_a_network_import_before_any_sandbox_opens(runner, settings) -> None:
    package = valid_package(
        tool="import socket\n\ndef run(args):\n    return {'text': args['text'], 'words': 0}\n"
    )
    report = verify(package, runner, settings)
    assert (
        report.status == "rejected"
        and statuses(report)["G1"] == "failed"
        and statuses(report)["G2"] == "not_run"
    )
    assert runner.specs == []


VACUOUS_TESTS = "from tool import run\n\n\ndef test_a():\n    assert True\n\n\ndef test_b():\n    run({'text': 'x'})\n\n\ndef test_c():\n    assert 1 + 1 == 2\n"


def test_g3_rejects_tests_that_check_nothing(runner, settings) -> None:
    report = verify(valid_package(tests=VACUOUS_TESTS), runner, settings)
    assert report.status == "rejected" and statuses(report)["G3"] == "failed"
    gate = next(g for g in report.gates if g.id == "G3")
    assert "do not constrain behaviour" in gate.summary and any(
        "assert on the returned values" in h for h in gate.hints
    )


def test_g3_rejects_a_failing_test_with_its_name_and_line(runner, settings) -> None:
    tests = (
        "from tool import run\n\n\n"
        "def test_ok():\n    assert run({'text': 'a b'})['words'] == 2\n\n\n"
        "def test_broken():\n    assert run({'text': 'a b'})['words'] == 3\n\n\n"
        "def test_ok_too():\n    assert run({'text': ''})['words'] == 0\n"
    )
    report = verify(valid_package(tests=tests), runner, settings)
    gate = next(g for g in report.gates if g.id == "G3")
    assert gate.status == "failed" and "1 of 3 tests did not pass" in gate.summary
    assert "test_broken" in gate.hints[0] and "tests/test_tool.py:" in gate.hints[0]


def test_g3_rejects_an_example_whose_output_is_wrong(runner, settings) -> None:
    package = valid_package(examples=[{"input": {"text": "a b c"}, "output": {"text": "a b c", "words": 2}}])
    report = verify(package, runner, settings)
    gate = next(g for g in report.gates if g.id == "G3")
    assert gate.status == "failed" and any("example 1 returned" in h for h in gate.hints)


ITEMS_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "integer"}, "maxItems": 5}},
    "required": ["items"],
    "additionalProperties": False,
}
ITEMS_TESTS = (
    "from tool import run\n\n\n"
    "def test_mean():\n    assert run({'items': [2, 4]}) == {'mean': 3.0}\n\n\n"
    "def test_single():\n    assert run({'items': [5]}) == {'mean': 5.0}\n\n\n"
    "def test_negative():\n    assert run({'items': [-1, 1]}) == {'mean': 0.0}\n"
)


def items_package(tool: str, **changes) -> ToolPackage:
    return valid_package(
        tool=tool,
        tests=ITEMS_TESTS,
        input_schema=ITEMS_SCHEMA,
        output_schema={"type": "object"},
        examples=[{"input": {"items": [1, 2, 3]}, "output": {"mean": 2.0}}],
        **changes,
    )


def test_g4_rejects_a_crash_on_an_empty_list_and_names_the_input(runner, settings) -> None:
    tool = "def run(args):\n    items = args['items']\n    return {'mean': sum(items) / len(items)}\n"
    report = verify(items_package(tool), runner, settings)
    assert report.status == "rejected" and statuses(report)["G3"] == "passed"
    gate = next(g for g in report.gates if g.id == "G4")
    assert gate.status == "failed" and "ZeroDivisionError" in gate.hints[0]
    assert "items = a list of 0" in gate.hints[0]


def test_g4_accepts_the_same_tool_when_it_refuses_the_empty_list_on_purpose(runner, settings) -> None:
    tool = (
        "from toolforge_sdk import ToolError\n\n\n"
        "def run(args):\n    items = args['items']\n    if not items:\n        raise ToolError('give at least one number')\n"
        "    return {'mean': sum(items) / len(items)}\n"
    )
    report = verify(items_package(tool), runner, settings)
    assert report.status == lifecycle.VERIFIED_BASIC, report.text()
    gate = next(g for g in report.gates if g.id == "G4")
    assert gate.metrics["handled_errors"] >= 1


def test_g4_rejects_a_hang(runner, settings) -> None:
    tool = (
        "import time\n\n\n"
        "def run(args):\n    items = args['items']\n    if not items:\n        time.sleep(60)\n"
        "    return {'mean': sum(items) / max(len(items), 1)}\n"
    )
    package = items_package(tool, limits={"timeout_s": 1, "memory_mb": 128, "output_kb": 16})
    report = verify(package, runner, settings)
    gate = next(g for g in report.gates if g.id == "G4")
    assert gate.status == "failed" and "Timeout" in gate.hints[0]


def test_g4_rejects_an_oversized_result(runner, settings) -> None:
    tool = (
        "def run(args):\n    items = args['items']\n    if len(items) >= 5:\n        return {'mean': 'x' * 100000}\n"
        "    return {'mean': sum(items) / max(len(items), 1)}\n"
    )
    report = verify(items_package(tool), runner, settings)
    gate = next(g for g in report.gates if g.id == "G4")
    assert gate.status == "failed" and any("OutputTooLarge" in h for h in gate.hints)


# --- Through the Forge and the registry ---------------------------------------------------------------------------------------------


def test_a_submission_is_registered_verified_and_leaves_rows_events_and_a_blob(runner, settings) -> None:
    ledgers = MemoryLedgers()
    with Registry.from_settings(settings) as registry:
        forge = Forge(settings, registry, runner, ledger_factory=ledgers)
        package = ToolPackage.from_dir(EXAMPLES / "word_count")
        report = forge.submit_package(package, "agent")
        record = registry.find_version("word_count", "1.0.0")
        assert report.status == record.status == lifecycle.VERIFIED_BASIC and report.registered
        assert [(r.gate, r.status) for r in registry.gate_results(record.id)] == [
            ("G1", "passed"), ("G2", "passed"), ("G3", "passed"), ("G4", "passed"), ("G5", "skipped"), ("G6", "skipped"),
        ]  # fmt: skip
        assert registry.blobs.get(record.blob_sha) == package.to_tar()
        assert ledgers.types[0] == "forge.submitted" and ledgers.types[-1] == "forge.verified"
        # the same content again: no new sandbox, the stored report
        before = len(runner.specs)
        again = forge.submit_package(package, "agent")
        assert again.status == report.status and len(runner.specs) == before
        assert [g.status for g in again.gates] == [g.status for g in report.gates]


def test_a_rejected_submission_is_recorded_and_its_version_cannot_be_reused(runner, settings) -> None:
    from toolforge.errors import RegistryError

    bad = valid_package(name="bad_tool", tests=VACUOUS_TESTS)
    with Registry.from_settings(settings) as registry:
        forge = Forge(settings, registry, runner, ledger_factory=MemoryLedgers())
        report = forge.submit_package(bad, "agent")
        assert report.status == "rejected" and registry.find_version("bad_tool", "0.1.0").status == "rejected"
        assert registry.find_version("bad_tool", "0.1.0").blob_sha  # G2 ran, so the blob exists
        assert "higher version" in report.next_action
        fixed = valid_package(name="bad_tool")
        with pytest.raises(RegistryError) as raised:
            forge.submit_package(fixed, "agent")
        assert raised.value.code == "version_exists" and "bump" in raised.value.hint
        fixed_v2 = valid_package(name="bad_tool", version="0.2.0", parent_version="0.1.0")
        assert forge.submit_package(fixed_v2, "agent").status == lifecycle.VERIFIED_BASIC


def test_the_ledger_events_hold_no_source_and_no_secret_text(runner, settings) -> None:
    ledgers = MemoryLedgers()
    package = valid_package(
        tool="def run(args):\n    return {'text': 'DISTINCTIVE_SOURCE_TEXT', 'words': 0}\n"
    )
    with Registry.from_settings(settings) as registry:
        Forge(settings, registry, runner, ledger_factory=ledgers).submit_package(package, "agent")
    assert "DISTINCTIVE_SOURCE_TEXT" not in json.dumps(ledgers.events)


# --- The Blast Radius report after a run (P08) ---------------------------------------------------------------------------------------


@pytest.mark.usefixtures("minilocker")
def test_a_finished_run_stores_a_blast_radius_report_that_does_not_claim_what_it_could_not_see(
    runner, settings
) -> None:
    with Registry(settings.home) as registry:
        report = Pipeline(settings, runner, registry, ledger_factory=ToolforgeLedger, fuzz_inputs=5).run(
            ToolPackage.from_dir(EXAMPLES / "word_count"), "test"
        )
        assert report.registered and report.status == lifecycle.VERIFIED_BASIC, report.text()
    stored = settings.home / "ledgers" / "word_count@1.0.0" / "blast-radius.json"
    assert stored.is_file()
    document = json.loads(stored.read_text())
    assert document["ledger_verified"] is True
    assert (
        document["verdict"]["status"] == "not_measured"
    )  # the stand-in runner audits nothing; no zeros are shown
    assert all(
        d["status"] == "not_measured" for name, d in document["dimensions"].items() if name != "persistence"
    )
    assert not [w for w in report.warnings if "Blast Radius" in w]


def test_an_in_memory_ledger_stores_no_report(runner, settings) -> None:
    ledgers = MemoryLedgers()
    with Registry(settings.home) as registry:
        Pipeline(settings, runner, registry, ledger_factory=ledgers, fuzz_inputs=5).run(
            ToolPackage.from_dir(EXAMPLES / "word_count"), "test"
        )
    assert not list((settings.home).glob("ledgers/**/blast-radius.json"))


@pytest.mark.usefixtures("minilocker")
def test_a_report_that_cannot_be_built_is_a_warning_not_a_failure(runner, settings, monkeypatch) -> None:
    from toolforge.report import blast_radius

    def boom(*args, **kwargs):
        raise RuntimeError("disk full: /secret/path")

    monkeypatch.setattr(blast_radius, "generate", boom)
    with Registry(settings.home) as registry:
        report = Pipeline(settings, runner, registry, ledger_factory=ToolforgeLedger, fuzz_inputs=5).run(
            ToolPackage.from_dir(EXAMPLES / "word_count"), "test"
        )
    assert report.status == lifecycle.VERIFIED_BASIC and report.registered
    (warning,) = [w for w in report.warnings if "Blast Radius" in w]
    assert "toolforge report word_count@1.0.0" in warning and "/secret/path" not in warning
