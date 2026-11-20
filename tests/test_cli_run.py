"""`toolforge run`: the command line in front of `ToolRunner`. The sandbox and the ledger are replaced by stand-ins."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from toolforge import cli_run
from toolforge.cli import main
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord
from toolforge.sandbox import FakeRunner, RunOutput

PACK = Path(__file__).resolve().parent.parent / "examples" / "toolpacks" / "word_count"


def envelope(nonce: str, **fields: object) -> bytes:
    body = {
        "ok": True,
        "result": {"words": 3},
        "error": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "duration_ms": 1,
        "truncated": False,
    }
    body.update(fields)
    text = json.dumps(body).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<<TF:{nonce}>>{text}<<TF-END:{nonce}>>\n".encode()


class Stand:
    """The fake sandbox and the ledger the command was given, and what it asked of them."""

    def __init__(self) -> None:
        self.fake = FakeRunner(lambda argv, files: RunOutput(0, envelope(argv[-1]), b"", False, False))
        self.shutdowns = 0
        self.ledger_paths: list[str | None] = []
        self.fake.shutdown = self._shutdown  # type: ignore[attr-defined]

    def _shutdown(self) -> None:
        self.shutdowns += 1

    def answer(self, make: Callable[[str], RunOutput]) -> None:
        self.fake.expect(lambda argv, files: make(argv[-1]))

    def files_of_last_call(self) -> Mapping[str, bytes]:
        return self.fake.handles[-1].files


@pytest.fixture
def stand(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> Stand:
    s = Stand()

    class Ledger:
        def __init__(self, path: str | None, task_id: str) -> None:
            s.ledger_paths.append(path)

        def append(self, actor: str, type: str, payload: dict | None = None) -> dict:
            return {}

    monkeypatch.setattr(cli_run, "make_sandbox_runner", lambda settings: s.fake)
    monkeypatch.setattr(cli_run, "make_ledger", lambda path, task_id: Ledger(path, task_id))
    return s


@pytest.fixture
def registry(tmp_home: Path) -> Iterator[Registry]:
    """Overrides the shared fixture: a registry in the directory the CLI opens through TOOLFORGE_HOME."""
    with Registry(tmp_home) as registry:
        yield registry


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["run", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- Refusing a bad request before anything starts -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        ([], "not both and not neither"),
        (["word_count", "--package", str(PACK), "--dev-unverified"], "not both and not neither"),
        (["--package", str(PACK)], "--dev-unverified"),
        (["word_count", "--dev-unverified"], "only applies to --package"),
        (["word_count", "--args", "{not json"], "not valid JSON"),
        (["word_count", "--args", "[1, 2]"], "must be a JSON object"),
        (["word_count", "--file", "/no/such/file.csv"], "is not a file"),
    ],
)
def test_a_bad_request_is_refused_with_a_message_and_nothing_opens(
    stand: Stand, capsys: pytest.CaptureFixture[str], argv: list[str], needle: str
) -> None:
    code, out, err = run_cli(capsys, *argv)
    assert code == 1 and out == ""
    assert "error [bad_run_request]" in err and needle in err and "hint:" in err
    assert stand.fake.opened == []


def test_two_files_with_the_same_name_are_refused(
    stand: Stand, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = tmp_path / "a" / "data.csv", tmp_path / "b" / "data.csv"
    first.write_text("x", encoding="utf-8")
    second.write_text("y", encoding="utf-8")
    code, _, err = run_cli(capsys, "word_count", "--file", str(first), "--file", str(second))
    assert code == 1 and "both called 'data.csv'" in err


def test_too_many_files_and_too_many_bytes_are_refused(
    stand: Stand, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    many = []
    for index in range(cli_run.MAX_FILES + 1):
        path = tmp_path / f"f{index}.txt"
        path.write_text("x", encoding="utf-8")
        many += ["--file", str(path)]
    assert "at most" in run_cli(capsys, "word_count", *many)[2]
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (cli_run.MAX_FILES_BYTES + 1))
    assert "larger than" in run_cli(capsys, "word_count", "--file", str(big))[2]


# --- A package straight from a folder ---------------------------------------------------------------------------------------------


def test_a_package_folder_runs_with_a_warning_and_prints_the_result_as_json(
    stand: Stand, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run_cli(
        capsys, "--package", str(PACK), "--args", '{"text": "a b c"}', "--dev-unverified"
    )
    assert code == 0
    assert err.strip() == cli_run.UNVERIFIED_WARNING
    printed = json.loads(out)
    assert printed["ok"] is True and printed["result"] == {"words": 3}
    assert printed["version"] == "word_count@1.0.0" and printed["error"] is None
    assert stand.shutdowns == 1
    assert "_dev" in str(stand.ledger_paths[0])  # nothing about this call is filed under a registered tool


def test_a_failed_call_exits_with_one_and_still_prints_the_result(
    stand: Stand, capsys: pytest.CaptureFixture[str]
) -> None:
    stand.answer(lambda nonce: RunOutput(0, b"", b"", True, False))
    code, out, _ = run_cli(capsys, "--package", str(PACK), "--args", '{"text": "a"}', "--dev-unverified")
    printed = json.loads(out)
    assert code == 1 and printed["ok"] is False and printed["error"]["type"] == "Timeout"
    assert printed["sandbox_timed_out"] is True and stand.shutdowns == 1


def test_arguments_that_do_not_match_the_schema_print_a_failed_result_without_a_sandbox(
    stand: Stand, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run_cli(capsys, "--package", str(PACK), "--args", '{"text": 5}', "--dev-unverified")
    assert code == 1 and json.loads(out)["error"]["type"] == "InvalidArguments"
    assert stand.fake.opened == []


def test_an_invalid_package_is_an_error_and_shuts_the_sandbox_runner_down(
    stand: Stand, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "empty").mkdir()
    code, _, err = run_cli(capsys, "--package", str(tmp_path / "empty"), "--dev-unverified")
    assert code == 1 and "error [package_invalid]" in err and "toolforge validate" in err
    assert stand.shutdowns == 1 and stand.fake.opened == []


def test_the_output_is_ascii_so_a_tool_cannot_move_the_operators_cursor(
    stand: Stand, capsys: pytest.CaptureFixture[str]
) -> None:
    stand.answer(
        lambda nonce: RunOutput(
            0, envelope(nonce, result={"words": "\u2713 \u202e"}, stdout_tail="\u2713"), b"", False, False
        )
    )
    code, out, _ = run_cli(capsys, "--package", str(PACK), "--args", '{"text": "a"}', "--dev-unverified")
    assert code == 0
    out.encode("ascii")
    assert json.loads(out)["result"] == {"words": "\u2713 \u202e"}


# --- A registered tool, by name -----------------------------------------------------------------------------------------------------


def register_active(
    registry: Registry,
    make_package: Callable[..., ToolPackage],
    activate: Callable[[VersionRecord], VersionRecord],
) -> VersionRecord:
    package = make_package()
    record = registry.add_version(package, created_by="test")
    registry.attach_blob(record.id, registry.blobs.put(package.to_tar()))
    return activate(record)


def test_a_registered_tool_runs_by_name_and_the_call_is_counted(
    stand: Stand,
    registry: Registry,
    make_package: Callable[..., ToolPackage],
    activate: Callable[[VersionRecord], VersionRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = register_active(registry, make_package, activate)
    code, out, err = run_cli(capsys, "word_count", "--args", '{"text": "a b c"}')
    assert code == 0 and err == ""  # no warning: this version went through the registry
    assert json.loads(out)["version"] == "word_count@1.0.0"
    assert registry.stats(record.id).uses == 1
    assert "word_count@1.0.0" in str(stand.ledger_paths[0]) and "_dev" not in str(stand.ledger_paths[0])


def test_a_registered_tool_that_is_not_active_is_an_error(
    stand: Stand,
    registry: Registry,
    make_package: Callable[..., ToolPackage],
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = make_package()
    record = registry.add_version(package, created_by="test")
    registry.attach_blob(record.id, registry.blobs.put(package.to_tar()))
    code, _, err = run_cli(capsys, "word_count@1.0.0", "--args", '{"text": "a"}')
    assert code == 1 and "error [status_not_allowed]" in err and "hint:" in err
    assert stand.fake.opened == []


def test_an_unknown_tool_is_an_error(stand: Stand, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_cli(capsys, "nothing_here")
    assert code == 1 and "error [unknown_version]" in err


def test_a_file_for_a_tool_that_takes_none_is_refused_before_anything_opens(
    stand: Stand,
    registry: Registry,
    make_package: Callable[..., ToolPackage],
    activate: Callable[[VersionRecord], VersionRecord],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    register_active(registry, make_package, activate)  # word_count: permissions.inputs is false
    data = tmp_path / "data.csv"
    data.write_text("a,b\n", encoding="utf-8")
    code, out, _ = run_cli(capsys, "word_count", "--args", '{"text": "a"}', "--file", str(data))
    assert code == 1 and json.loads(out)["error"]["type"] == "InvalidFiles"
    assert stand.fake.opened == []


def test_files_are_given_to_the_tool_under_inputs(
    stand: Stand, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data.csv"
    data.write_bytes(b"a,b\n1,2\n")
    csv_pack = PACK.parent / "csv_summary"
    code, out, _ = run_cli(
        capsys,
        "--package",
        str(csv_pack),
        "--args",
        '{"path": "inputs/data.csv"}',
        "--file",
        str(data),
        "--dev-unverified",
    )
    assert code == 0 and json.loads(out)["ok"] is True
    assert stand.files_of_last_call()["/workspace/inputs/data.csv"] == b"a,b\n1,2\n"


def test_run_is_listed_in_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    assert "run" in capsys.readouterr().out.split("<command>")[-1]
