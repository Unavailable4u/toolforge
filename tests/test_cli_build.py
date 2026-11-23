"""`toolforge build`: the command in front of the builder. The model is a scripted `FakeLLM`; the sandbox is the stand-in that runs the real
harness on checked-in trusted code (`tests/local_sandbox.py`), so the real gates G1 to G4 judge the package the model "wrote"; the ledger is
replaced so that no file is written outside the temporary home."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from local_sandbox import LocalRunner
from verify_helpers import MemoryLedgers

from toolforge import cli_build, cli_run
from toolforge.cli import main
from toolforge.errors import LLMUnavailable, ValidationError
from toolforge.llm.client import FakeLLM
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")
GOAL = "tally the words in some text"


def reply_from_pack(name: str = "word_count") -> dict:
    """The reference pack, as a model would answer: no `version`, no `parent_version`."""
    pack = ToolPackage.from_dir(EXAMPLES / name)
    manifest = pack.manifest.model_dump(mode="json")
    manifest.pop("version", None)
    manifest.pop("parent_version", None)
    return {"manifest": manifest, "files": dict(pack.files)}


class Stand:
    def __init__(self) -> None:
        self.runner = LocalRunner()
        self.ledgers = MemoryLedgers()
        self.llm: FakeLLM | None = FakeLLM([reply_from_pack()])
        self.shutdowns = 0
        self.runner.shutdown = self._shutdown  # type: ignore[attr-defined]

    def _shutdown(self) -> None:
        self.shutdowns += 1


@pytest.fixture
def stand(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    s = Stand()
    monkeypatch.setattr(cli_run, "make_sandbox_runner", lambda settings: s.runner)
    monkeypatch.setattr(cli_run, "make_ledger", s.ledgers)
    monkeypatch.setattr(cli_build, "client_from_settings", lambda settings, role: s.llm)
    yield s
    s.runner.cleanup()


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- a build ---------------------------------------------------------------------------------------------------------------------


def test_a_build_registers_a_tool_by_the_builder_and_says_what_the_operator_does_next(
    stand, tmp_home, capsys
) -> None:
    code, out, err = run_cli(capsys, "build", GOAL)
    assert code == 0, out + err
    assert (
        "word_count@0.1.0 (verified_basic)" in out
        and 'toolforge promote word_count@0.1.0 --reason "<why>"' in out
    )
    assert all(g in out for g in ("rounds: 1 of 4", "tokens:"))
    with Registry(tmp_home) as registry:
        record = registry.find_version("word_count", "0.1.0")
        assert record.status == lifecycle.VERIFIED_BASIC and record.created_by == "builder"
        assert registry.active_version("word_count") is None
        assert len(registry.gate_results(record.id)) == 6
    assert stand.shutdowns == 1
    assert [e[2] for e in stand.ledgers.events if e[1] == "builder"] == ["forge.build.finished"]


def test_progress_goes_to_stderr_and_quiet_turns_it_off(stand, capsys) -> None:
    _, out, err = run_cli(capsys, "build", GOAL)
    assert "round 1: asking the model" in err and "G3" in err and "asking the model" not in out
    stand.llm = FakeLLM([reply_from_pack("csv_summary")])
    _, _, quiet = run_cli(capsys, "build", GOAL, "--quiet")
    assert quiet == ""


def test_json_output_is_one_valid_document_with_the_report(stand, capsys) -> None:
    code, out, _ = run_cli(capsys, "build", GOAL, "--json", "--quiet")
    data = json.loads(out)
    assert (
        code == 0
        and data["ok"] is True
        and data["stop_reason"] == "verified"
        and data["tool"] == "word_count"
    )
    assert [g["id"] for g in data["report"]["gates"]] == ["G1", "G2", "G3", "G4", "G5", "G6"]
    assert data["usage"]["requests"] == 1 and data["attempts"][0]["failed_gate"] is None


def test_the_examples_and_samples_reach_the_model_prompt(stand, tmp_path, capsys) -> None:
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps([{"input": {"text": "a b"}, "output": {"words": 2}}]))
    sample = tmp_path / "notes.txt"
    sample.write_text("first line\nsecond line\n")
    code, _, err = run_cli(
        capsys,
        "build",
        GOAL,
        "--examples",
        str(examples),
        "--sample",
        str(sample),
        "--constraints",
        "no network",
    )
    assert code == 0, err
    prompt = stand.llm.requests[0][1]["content"]
    assert (
        '"words": 2' in prompt
        and "inputs/notes.txt" in prompt
        and "second line" in prompt
        and "no network" in prompt
    )


def test_a_build_that_fails_exits_one_and_prints_why(stand, capsys) -> None:
    stand.llm = FakeLLM([LLMUnavailable("the provider answered HTTP 503")])
    code, out, _ = run_cli(capsys, "build", GOAL)
    assert code == 1 and "no tool was built" in out and "HTTP 503" in out and "next:" in out
    assert stand.shutdowns == 1


def test_a_model_that_returns_a_bad_tool_is_told_and_gives_up_without_activating_anything(
    stand, tmp_home, capsys
) -> None:
    broken = reply_from_pack()
    broken["files"]["tool.py"] = (
        "import socket\n\ndef run(args):\n    return {}\n"  # G1 rejects a network import
    )
    stand.llm = FakeLLM([broken, broken])
    code, out, _ = run_cli(capsys, "build", GOAL)
    assert (
        code == 1 and "same failure came twice" in out and "rejected at G1" in out and out.count("next:") == 1
    )
    assert len(stand.llm.requests) == 2 and "Gate G1" in stand.llm.requests[1][1]["content"]
    with Registry(tmp_home) as registry:
        assert [v.status for v in registry.list_versions("word_count")] == [lifecycle.REJECTED] * 2
        assert registry.active_version("word_count") is None


# --- what is refused before anything runs ----------------------------------------------------------------------------------------


def test_without_a_model_the_command_says_what_to_set(stand, capsys) -> None:
    stand.llm = None
    code, out, err = run_cli(capsys, "build", GOAL)
    assert code == 1 and out == "" and "TOOLFORGE_LLM_BASE_URL" in err
    assert stand.shutdowns == 1


def test_a_credential_in_the_goal_is_refused_and_never_printed(stand, capsys) -> None:
    key = "AKIA" + "QWERTYUIOP" + "ASDFGH"
    code, out, err = run_cli(capsys, "build", f"call the service with {key}")
    assert code == 1 and "secret_in_request" in err and key not in out + err
    assert stand.llm.requests == [] and stand.shutdowns == 1


def test_a_bad_examples_file_is_explained_before_a_sandbox_opens(stand, tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code, out, err = run_cli(capsys, "build", GOAL, "--examples", str(bad))
    assert code == 1 and "bad_option" in err and "could not be read as JSON" in err and out == ""
    assert stand.runner.handles == []
    not_a_list = tmp_path / "obj.json"
    not_a_list.write_text('{"input": {}}')
    code, _, err = run_cli(capsys, "build", GOAL, "--examples", str(not_a_list))
    assert code == 1 and "not a JSON list" in err


def test_a_sample_that_is_missing_binary_or_large_is_refused(stand, tmp_path, capsys) -> None:
    missing = run_cli(capsys, "build", GOAL, "--sample", str(tmp_path / "nope.txt"))
    assert missing[0] == 1 and "could not be read" in missing[2]
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    assert run_cli(capsys, "build", GOAL, "--sample", str(binary))[0] == 1
    big = tmp_path / "big.txt"
    big.write_text("x" * (21 * 1024))
    code, _, err = run_cli(capsys, "build", GOAL, "--sample", str(big))
    assert code == 1 and "larger than 20 KB" in err
    assert stand.llm.requests == []


def test_read_samples_keys_by_file_name_and_refuses_two_of_the_same_name(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "rows.csv").write_text("1,2\n")
    (tmp_path / "b" / "rows.csv").write_text("3,4\n")
    assert cli_build.read_samples([]) is None
    assert cli_build.read_samples([str(tmp_path / "a" / "rows.csv")]) == {"rows.csv": "1,2\n"}
    with pytest.raises(ValidationError) as caught:
        cli_build.read_samples([str(tmp_path / "a" / "rows.csv"), str(tmp_path / "b" / "rows.csv")])
    assert "both called 'rows.csv'" in caught.value.message


def test_read_examples_passes_a_list_through_and_none_through(tmp_path) -> None:
    assert cli_build.read_examples(None) is None
    path = tmp_path / "e.json"
    path.write_text('[{"input": {"text": ""}, "output": {"words": 0}}]')
    assert cli_build.read_examples(str(path)) == [{"input": {"text": ""}, "output": {"words": 0}}]


def test_the_command_is_listed_in_the_help(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "build" in capsys.readouterr().out
