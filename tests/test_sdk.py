"""`toolforge.sdk`, the file that is written into every sandbox as `toolforge_sdk.py` (docs/guide/package-format.md, "How a tool runs")."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from toolforge import sdk
from toolforge.sdk import ToolError, input_path, output_path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sdk, "_WORKSPACE", str(tmp_path))
    (tmp_path / "inputs" / "sub").mkdir(parents=True)
    (tmp_path / "inputs" / "data.csv").write_text("a,b\n", encoding="utf-8")
    (tmp_path / "inputs" / "sub" / "x.txt").write_text("x", encoding="utf-8")
    return tmp_path


def test_tool_error_is_an_exception_with_the_message() -> None:
    assert issubclass(ToolError, Exception)
    assert str(ToolError("not a CSV")) == "not a CSV"


def test_input_path_resolves_under_inputs(workspace: Path) -> None:
    assert input_path("data.csv") == f"{workspace}/inputs/data.csv"
    assert input_path("sub/x.txt") == f"{workspace}/inputs/sub/x.txt"


def test_input_path_accepts_one_leading_inputs_so_the_argument_a_caller_sent_works(workspace: Path) -> None:
    assert input_path("inputs/data.csv") == input_path("data.csv")
    with pytest.raises(ToolError):
        input_path("inputs/inputs/data.csv")  # only one prefix is removed


@pytest.mark.parametrize(
    "name",
    ["", " ", "/etc/passwd", "../x", "a/../b", "a//b", "a/", ".", "./data.csv", "a\\b", "a\x00b", None, 5],
)
def test_input_path_refuses_names_that_are_not_plain_relative_paths(workspace: Path, name: object) -> None:
    with pytest.raises(ToolError):
        input_path(name)  # type: ignore[arg-type]


def test_input_path_says_what_was_provided_when_the_file_is_missing(workspace: Path) -> None:
    with pytest.raises(ToolError) as info:
        input_path("nope.csv")
    message = str(info.value)
    assert "nope.csv" in message and "'data.csv'" in message and "'sub/x.txt'" in message


def test_input_path_says_so_when_nothing_was_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdk, "_WORKSPACE", str(tmp_path))
    with pytest.raises(ToolError, match="no files were provided"):
        input_path("data.csv")


def test_input_path_lists_at_most_twenty_names(workspace: Path) -> None:
    for index in range(25):
        (workspace / "inputs" / f"f{index:02}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ToolError) as info:
        input_path("nope")
    assert "more" in str(info.value) and str(info.value).count(".txt") <= 20


def test_input_path_refuses_a_link_that_leaves_the_inputs_folder(workspace: Path, tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "inputs" / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available here")
    with pytest.raises(ToolError):
        input_path("link.txt")


def test_input_path_refuses_a_folder(workspace: Path) -> None:
    with pytest.raises(ToolError):
        input_path("sub")


def test_output_path_is_under_outputs_and_creates_its_folders_but_not_the_file(workspace: Path) -> None:
    path = output_path("reports/out.json")
    assert path == f"{workspace}/outputs/reports/out.json"
    assert (workspace / "outputs" / "reports").is_dir() and not Path(path).exists()


@pytest.mark.parametrize("name", ["", "/tmp/x", "../x", "a/../x", "a\\b"])
def test_output_path_refuses_names_that_are_not_plain_relative_paths(workspace: Path, name: str) -> None:
    with pytest.raises(ToolError):
        output_path(name)


def test_the_module_is_standard_library_only_and_does_not_import_toolforge() -> None:
    tree = ast.parse(Path(sdk.__file__).read_text(encoding="utf-8"))
    roots = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    roots |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert roots <= {"__future__", "os", "posixpath"}


def test_the_public_names_are_the_documented_ones() -> None:
    assert sorted(sdk.__all__) == ["ToolError", "input_path", "output_path"]
