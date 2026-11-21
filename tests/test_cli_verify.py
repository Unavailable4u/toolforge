"""`toolforge verify` and `toolforge submit`: the commands in front of the pipeline. The sandbox is a stand-in that runs the real harness
on checked-in trusted code (`tests/local_sandbox.py`); the ledger is replaced so that no file is written outside the temporary home."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from local_sandbox import LocalRunner
from verify_helpers import MemoryLedgers

from toolforge import cli_run
from toolforge.cli import main
from toolforge.registry import Registry, lifecycle

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")


class Stand:
    def __init__(self) -> None:
        self.runner = LocalRunner()
        self.ledgers = MemoryLedgers()
        self.shutdowns = 0
        self.runner.shutdown = self._shutdown  # type: ignore[attr-defined]

    def _shutdown(self) -> None:
        self.shutdowns += 1


@pytest.fixture
def stand(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    s = Stand()
    monkeypatch.setattr(cli_run, "make_sandbox_runner", lambda settings: s.runner)
    monkeypatch.setattr(cli_run, "make_ledger", s.ledgers)
    yield s
    s.runner.cleanup()


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- verify (a dry run) ----------------------------------------------------------------------------------------------------------


def test_verify_prints_the_report_and_exits_zero_for_a_good_pack(stand, tmp_home, capsys) -> None:
    code, out, err = run_cli(capsys, "verify", str(EXAMPLES / "word_count"))
    assert code == 0, out + err
    assert "verified_basic" in out and all(g in out for g in ("G1", "G2", "G3", "G4"))
    assert stand.shutdowns == 1


def test_verify_writes_nothing_to_the_registry_or_the_ledger(stand, tmp_home, capsys) -> None:
    run_cli(capsys, "verify", str(EXAMPLES / "word_count"))
    assert stand.ledgers.events == []
    with Registry(tmp_home) as registry:
        assert registry.list_versions() == []


def test_verify_json_is_valid_json_with_the_gates(stand, capsys) -> None:
    code, out, _ = run_cli(capsys, "verify", str(EXAMPLES / "csv_summary"), "--json")
    data = json.loads(out)
    assert code == 0 and data["status"] == "verified_basic" and data["dry_run"] is True
    assert [g["id"] for g in data["gates"]] == ["G1", "G2", "G3", "G4", "G5", "G6"]


def test_verify_exits_one_for_a_rejected_package(stand, tmp_path, capsys) -> None:
    from verify_helpers import valid_package

    folder = tmp_path / "bad"
    for name, text in valid_package(tool="import socket\n\ndef run(args):\n    return {}\n").files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (folder / "manifest.json").write_text(
        json.dumps(valid_package(tool="import socket\n").manifest.model_dump(mode="json"))
    )
    code, out, _ = run_cli(capsys, "verify", str(folder))
    assert code == 1 and "rejected" in out and "G1" in out


@pytest.mark.parametrize("value", ["0", "-1", "201"])
def test_a_bad_fuzz_input_count_is_refused_before_a_sandbox_opens(stand, capsys, value) -> None:
    code, out, err = run_cli(capsys, "verify", str(EXAMPLES / "word_count"), "--fuzz-inputs", value)
    assert code == 1 and out == "" and "--fuzz-inputs must be from 1 to 200" in err
    assert stand.runner.handles == [] and stand.shutdowns == 1


def test_fuzz_inputs_are_passed_to_g4(stand, capsys) -> None:
    code, out, _ = run_cli(capsys, "verify", str(EXAMPLES / "word_count"), "--fuzz-inputs", "5", "--json")
    g4 = next(g for g in json.loads(out)["gates"] if g["id"] == "G4")
    assert code == 0 and g4["metrics"]["inputs_planned"] == 5


# --- submit ----------------------------------------------------------------------------------------------------------------------


def test_submit_registers_and_verifies_and_records_who_submitted(stand, tmp_home, capsys) -> None:
    code, out, err = run_cli(capsys, "submit", str(EXAMPLES / "word_count"), "--by", "alice")
    assert code == 0, out + err
    with Registry(tmp_home) as registry:
        record = registry.find_version("word_count", "1.0.0")
        assert record.status == lifecycle.VERIFIED_BASIC and record.created_by == "alice"
        assert len(registry.gate_results(record.id)) == 6
    assert "forge.verified" in stand.ledgers.types and stand.shutdowns == 1


def test_submitting_the_same_pack_twice_gives_the_same_verdict(stand, tmp_home, capsys) -> None:
    run_cli(capsys, "submit", str(EXAMPLES / "word_count"))
    opened = len(stand.runner.handles)
    code, out, _ = run_cli(capsys, "submit", str(EXAMPLES / "word_count"), "--json")
    assert code == 0 and json.loads(out)["status"] == "verified_basic"
    assert len(stand.runner.handles) == opened


def test_the_default_submitter_is_the_operator(stand, tmp_home, capsys) -> None:
    run_cli(capsys, "submit", str(EXAMPLES / "word_count"))
    with Registry(tmp_home) as registry:
        assert registry.find_version("word_count", "1.0.0").created_by == "operator"


def test_the_sandbox_is_shut_down_even_when_the_run_fails(stand, tmp_home, capsys, monkeypatch) -> None:
    from toolforge.errors import SandboxUnavailable

    def refuse(spec):
        raise SandboxUnavailable("no docker", code="docker_unavailable", hint="start Docker")

    monkeypatch.setattr(stand.runner, "open", refuse)
    code, out, err = run_cli(capsys, "submit", str(EXAMPLES / "word_count"))
    assert code == 1 and "docker_unavailable" in err and stand.shutdowns == 1
    with Registry(tmp_home) as registry:  # the version waits, to be resumed by submitting again
        assert registry.find_version("word_count", "1.0.0").status == lifecycle.VERIFYING


def test_a_missing_folder_is_explained(stand, capsys, tmp_path) -> None:
    code, out, err = run_cli(capsys, "verify", str(tmp_path / "nothing"))
    assert code == 1 and "error" in err


def test_a_fuzz_input_count_that_is_not_a_number_is_refused_by_the_parser(stand) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["verify", str(EXAMPLES / "word_count"), "--fuzz-inputs", "many"])
    assert raised.value.code == 2 and stand.runner.handles == []
