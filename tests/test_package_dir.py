"""Reading and writing package directories: hostile directories become issues, never exceptions or file reads outside."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from toolforge.config import Settings
from toolforge.errors import ToolForgeError
from toolforge.models.package import ToolPackage
from toolforge.packaging import MAX_WALK_ENTRIES, write_package_dir

pytestmark = pytest.mark.usefixtures("inline_schema_check")
Payload = dict[str, Any]


@pytest.fixture
def pack(tmp_path: Path, make_payload: Callable[[], Payload]) -> Path:
    payload = make_payload()
    root = tmp_path / "pack"
    write_package_dir(root, json.dumps(payload["manifest"], indent=2), payload["files"])
    return root


def _codes(root: Path, settings: Settings) -> set[str]:
    return {issue.code for issue in ToolPackage.from_dir(root).validate(settings).issues}


def test_a_valid_directory_validates(pack: Path, settings: Settings) -> None:
    assert ToolPackage.from_dir(pack).validate(settings).ok


def test_missing_directory_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(ToolForgeError) as caught:
        ToolPackage.from_dir(tmp_path / "nope")
    assert caught.value.code == "package_not_found" and caught.value.hint


def test_a_file_is_not_a_package_directory(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    with pytest.raises(ToolForgeError):
        ToolPackage.from_dir(tmp_path / "f")


def test_missing_manifest(pack: Path, settings: Settings) -> None:
    (pack / "manifest.json").unlink()
    assert "manifest.missing" in _codes(pack, settings)


def test_invalid_json_names_the_line(pack: Path, settings: Settings) -> None:
    (pack / "manifest.json").write_text('{\n  "name": "x",\n  oops\n}\n')
    report = ToolPackage.from_dir(pack).validate(settings)
    issue = next(i for i in report.issues if i.code == "manifest.invalid_json")
    assert "line 3" in issue.message
    assert "manifest.missing" not in {i.code for i in report.issues}


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_json_constants_that_are_not_json_are_rejected(pack: Path, settings: Settings, literal: str) -> None:
    (pack / "manifest.json").write_text('{"name": %s}' % literal)
    assert "manifest.invalid_json" in _codes(pack, settings)


def test_deeply_nested_manifest_json_does_not_crash(pack: Path, settings: Settings) -> None:
    (pack / "manifest.json").write_text("[" * 200000 + "]" * 200000)
    assert _codes(pack, settings) & {"manifest.invalid_json", "manifest.too_deep"}


def test_manifest_that_is_not_an_object(pack: Path, settings: Settings) -> None:
    (pack / "manifest.json").write_text("[1, 2, 3]")
    assert "manifest.not_object" in _codes(pack, settings)


def test_invalid_utf8_is_reported_not_raised(pack: Path, settings: Settings) -> None:
    (pack / "data.txt").write_bytes(b"\xff\xfe\x00bad")
    assert "file.encoding" in _codes(pack, settings)


def test_invalid_utf8_manifest_is_one_clear_issue(pack: Path, settings: Settings) -> None:
    (pack / "manifest.json").write_bytes(b"\xff\xfe{}")
    assert _codes(pack, settings) == {"file.encoding"}


def test_oversized_file_is_not_read_into_memory(pack: Path, settings: Settings) -> None:
    (pack / "big.txt").write_text("x" * (64 * 1024 + 1))
    package = ToolPackage.from_dir(pack)
    assert "big.txt" not in package.files
    assert "file.too_large" in {i.code for i in package.validate(settings).issues}


def test_symlinks_are_refused_and_not_followed(pack: Path, settings: Settings, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET CONTENT")
    os.symlink(secret, pack / "link.txt")
    os.symlink(tmp_path, pack / "linked_dir")
    package = ToolPackage.from_dir(pack)
    assert "link.txt" not in package.files and not any("secret" in t.lower() for t in package.files.values())
    report = package.validate(settings)
    assert {"link.txt", "linked_dir"} <= {i.path for i in report.issues if i.code == "file.path"}
    assert "TOP SECRET" not in report.model_dump_json()


def test_a_symlinked_manifest_is_refused(pack: Path, settings: Settings, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text((pack / "manifest.json").read_text())
    (pack / "manifest.json").unlink()
    os.symlink(real, pack / "manifest.json")
    codes = _codes(pack, settings)
    assert "file.path" in codes


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
def test_a_named_pipe_does_not_hang_the_reader(pack: Path, settings: Settings) -> None:
    os.mkfifo(pack / "pipe.txt")
    assert "file.path" in _codes(pack, settings)


def test_tool_caches_are_ignored(pack: Path, settings: Settings) -> None:
    (pack / "__pycache__").mkdir()
    (pack / "__pycache__" / "tool.cpython-312.pyc").write_bytes(b"\x00\x01")
    (pack / ".pytest_cache").mkdir()
    (pack / ".pytest_cache" / "x").write_text("x")
    (pack / ".DS_Store").write_bytes(b"\x00")
    assert ToolPackage.from_dir(pack).validate(settings).ok


def test_hidden_files_are_refused(pack: Path, settings: Settings) -> None:
    (pack / ".env").write_text("KEY=1\n")
    assert "file.path" in _codes(pack, settings)


def test_too_many_entries_stops_the_walk(pack: Path, settings: Settings) -> None:
    for index in range(MAX_WALK_ENTRIES + 20):
        (pack / f"f{index}.txt").write_text("x")
    package = ToolPackage.from_dir(pack)
    assert len(package.files) <= MAX_WALK_ENTRIES
    assert "files.too_many" in {i.code for i in package.validate(settings).issues}


def test_deep_nesting_is_refused(pack: Path, settings: Settings) -> None:
    deep = pack
    for index in range(12):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)
    (deep / "x.txt").write_text("x")
    assert "file.path" in _codes(pack, settings)


def test_crlf_is_read_without_translation(pack: Path) -> None:
    (pack / "data.txt").write_bytes(b"a\r\nb\r\n")
    assert ToolPackage.from_dir(pack).files["data.txt"] == "a\r\nb\r\n"


def test_unreadable_directory_is_an_issue(pack: Path, settings: Settings) -> None:
    locked = pack / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as a user that ignores permissions")
        assert "file.path" in _codes(pack, settings)
    finally:
        locked.chmod(0o755)


# --- writing -----------------------------------------------------------------------------------------------------------


def test_write_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    with pytest.raises(ToolForgeError) as caught:
        write_package_dir(target, "{}", {"tool.py": "x = 1\n"})
    assert caught.value.code == "package_exists"
    assert (target / "keep.txt").read_text() == "mine" and not (target / "tool.py").exists()


def test_write_accepts_an_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.mkdir()
    write_package_dir(target, "{}", {"a/b/tool.py": "x = 1\n"})
    assert (target / "a" / "b" / "tool.py").read_text() == "x = 1\n"


@pytest.mark.parametrize(
    "name", ["../escape.py", "/abs/x.py", "a/../../x.py", "a\\b.py", ".hidden.py", "a//b.py"]
)
def test_write_refuses_unsafe_paths_and_writes_nothing(tmp_path: Path, name: str) -> None:
    target = tmp_path / "t"
    with pytest.raises(ToolForgeError) as caught:
        write_package_dir(target, "{}", {"ok.py": "x = 1\n", name: "x = 1\n"})
    assert caught.value.code == "unsafe_path"
    assert not target.exists(), "paths are checked before anything is written"
    assert not (tmp_path / "escape.py").exists()


def test_write_refuses_non_text_content(tmp_path: Path) -> None:
    with pytest.raises(ToolForgeError):
        write_package_dir(tmp_path / "t", "{}", {"a.txt": "a\x00b"})
