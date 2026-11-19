"""`toolforge validate` and `toolforge new`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from toolforge.cli import main
from toolforge.models.package import ToolPackage

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"


def test_validate_a_good_package(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLES / "word_count")]) == 0
    assert capsys.readouterr().out.strip() == "valid: 0 errors, 0 warnings"


def test_validate_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLES / "csv_summary"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"issues": [], "warnings": [], "ok": True}


def _broken(tmp_path: Path) -> Path:
    main(["new", "broken_tool", "--dir", str(tmp_path)])
    folder = tmp_path / "broken_tool"
    manifest = json.loads((folder / "manifest.json").read_text())
    del manifest["output_schema"]  # advice only, and the manifest stays valid so the advice is shown
    (folder / "manifest.json").write_text(json.dumps(manifest))
    (folder / "run.sh").write_text("echo hi\n")  # the one error
    return folder


def test_validate_prints_errors_with_hints_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _broken(tmp_path)
    capsys.readouterr()
    assert main(["validate", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "error [file.extension] run.sh:" in out
    assert "  hint: Rename or remove the file; allowed extensions are" in out
    assert "warning [output_schema.missing]" in out
    assert out.strip().endswith("INVALID: 1 error, 1 warning")


def test_validate_json_reports_issues_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = _broken(tmp_path)
    capsys.readouterr()
    assert main(["validate", str(folder), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert [i["code"] for i in report["issues"]] == ["file.extension"]
    assert set(report["issues"][0]) == {"path", "code", "message", "hint"}
    assert [w["code"] for w in report["warnings"]] == ["output_schema.missing"]


def test_validate_a_missing_folder_is_a_readable_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(tmp_path / "nope")]) == 1
    err = capsys.readouterr().err
    assert "error [package_not_found]" in err and "hint:" in err and "Traceback" not in err


def test_validate_never_runs_package_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The entrypoint, a helper and a test all try to leave a marker if anything imports or executes them."""
    marker = tmp_path / "EXECUTED"
    main(["new", "trap_tool", "--dir", str(tmp_path)])
    folder = tmp_path / "trap_tool"
    trap = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    (folder / "tool.py").write_text(trap + "\n\ndef run(args):\n    return {}\n")
    (folder / "helper.py").write_text(trap)
    (folder / "tests" / "test_tool.py").write_text(
        trap + "\n\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n"
    )
    (folder / "conftest.py").write_text(trap)
    capsys.readouterr()
    main(["validate", str(folder)])
    assert not marker.exists(), "validate executed package code"
    assert "conftest.py" in ToolPackage.from_dir(folder).files  # read as text like any file, never run


def test_validate_does_not_leave_files_behind(tmp_path: Path) -> None:
    main(["new", "clean_tool", "--dir", str(tmp_path)])
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    main(["validate", str(tmp_path / "clean_tool")])
    assert before == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))


# --- new -------------------------------------------------------------------------------------------------------------------


def test_new_scaffold_validates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "demo_tool", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    folder = tmp_path / "demo_tool"
    assert f"created {folder}" in out and f"next: toolforge validate {folder}" in out
    assert main(["validate", str(folder)]) == 0


def test_new_scaffold_has_three_real_tests(tmp_path: Path) -> None:
    main(["new", "demo_tool", "--dir", str(tmp_path)])
    text = (tmp_path / "demo_tool" / "tests" / "test_tool.py").read_text()
    assert text.count("\ndef test_") + text.startswith("def test_") == 3
    assert text.count("assert ") >= 4 and "== {" in text, "tests must compare returned values"


def _run_scaffold_tests(source: str, run: object) -> list[str]:
    """Run the scaffold's tests against `run`; return the names of the tests that failed. Our own template, not package code."""
    namespace: dict = {"run": run}
    exec(compile(source.replace("from tool import run\n", ""), "scaffold_tests", "exec"), namespace)
    failed = []
    for name, test in namespace.items():
        if name.startswith("test_") and callable(test):
            try:
                test()
            except Exception:
                failed.append(name)
    return failed


def test_new_scaffold_tests_pass_on_the_tool_and_fail_on_a_stub(tmp_path: Path) -> None:
    """The tests constrain behaviour: they pass on the real tool and all fail when it returns None (what G3 will check)."""
    main(["new", "demo_tool", "--dir", str(tmp_path)])
    folder = tmp_path / "demo_tool"
    namespace: dict = {}
    exec(compile((folder / "tool.py").read_text(), "scaffold_tool", "exec"), namespace)  # our own template
    source = (folder / "tests" / "test_tool.py").read_text()
    assert _run_scaffold_tests(source, namespace["run"]) == []
    assert len(_run_scaffold_tests(source, lambda args: None)) == 3


def test_new_uses_the_current_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["new", "here_tool"]) == 0
    assert (tmp_path / "here_tool" / "manifest.json").is_file()


@pytest.mark.parametrize("name", ["Bad", "ab", "9lives", "has-dash", "forge_x", "tf_x", "a" * 42, ""])
def test_new_rejects_bad_names_and_writes_nothing(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", name, "--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "error [name." in err and "hint:" in err and "Traceback" not in err
    assert list(tmp_path.iterdir()) == []


def test_new_refuses_to_overwrite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "taken_tool").mkdir()
    (tmp_path / "taken_tool" / "mine.txt").write_text("keep me")
    assert main(["new", "taken_tool", "--dir", str(tmp_path)]) == 1
    assert "error [package_exists]" in capsys.readouterr().err
    assert (tmp_path / "taken_tool" / "mine.txt").read_text() == "keep me"


def test_the_commands_are_listed_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "validate" in out and "new" in out


def test_commands_survive_a_hostile_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOOLFORGE_MAX_PACKAGE_KB", "1")
    main(["new", "small_tool", "--dir", str(tmp_path)])
    assert main(["validate", str(tmp_path / "small_tool")]) in {0, 1}
    assert os.environ["TOOLFORGE_MAX_PACKAGE_KB"] == "1"
