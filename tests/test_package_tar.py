"""Determinism of `to_tar` and `code_sha`, and agreement between `from_dir` and `from_submission`."""

from __future__ import annotations

import copy
import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from toolforge.config import Settings
from toolforge.errors import ValidationError
from toolforge.models.package import ToolPackage

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"
Payload = dict[str, Any]


def _package(payload: Payload) -> ToolPackage:
    return ToolPackage.from_submission(payload)


def test_same_content_in_a_different_order_gives_the_same_hash(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    reordered = {
        "files": dict(reversed(list(payload["files"].items()))),
        "manifest": dict(reversed(list(copy.deepcopy(payload["manifest"]).items()))),
    }
    assert _package(payload).to_tar() == _package(reordered).to_tar()
    assert _package(payload).code_sha == _package(reordered).code_sha


def test_one_changed_byte_changes_the_hash(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    changed = make_payload()
    changed["files"]["tool.py"] = payload["files"]["tool.py"].replace("words", "wordz", 1)
    assert _package(payload).code_sha != _package(changed).code_sha


def test_a_manifest_change_changes_the_hash(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    changed = make_payload()
    changed["manifest"]["version"] = "1.0.1"
    assert _package(payload).code_sha != _package(changed).code_sha


def test_omitting_a_default_does_not_change_the_hash(make_payload: Callable[[], Payload]) -> None:
    explicit = make_payload()
    implicit = make_payload()
    for key in ("parent_version", "kind", "requirements"):
        implicit["manifest"].pop(key)
    implicit["manifest"]["examples"][0].pop("live")
    assert _package(explicit).code_sha == _package(implicit).code_sha


def test_hash_is_stable_across_runs_and_machines(make_payload: Callable[[], Payload]) -> None:
    # A golden value: if this changes, every stored `code_sha` and approval bound to one would stop matching.
    assert _package(make_payload()).code_sha == GOLDEN_SHA


def test_tar_entries_are_normalised(make_payload: Callable[[], Payload]) -> None:
    data = _package(make_payload()).to_tar()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert members[0].name == "manifest.json"
    for member in members:
        assert member.isreg()
        assert (member.mtime, member.uid, member.gid, member.mode) == (0, 0, 0, 0o644)
        assert member.uname == "" and member.gname == ""


def test_tar_round_trips_the_content(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    data = _package(payload).to_tar()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        extracted = {m.name: archive.extractfile(m).read().decode("utf-8") for m in archive.getmembers()}
    assert json.loads(extracted.pop("manifest.json"))["name"] == "word_count"
    assert extracted == payload["files"]


def test_unicode_file_names_and_text_survive(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    payload["files"]["data/café.txt"] = "crème brûlée\n"
    package = _package(payload)
    with tarfile.open(fileobj=io.BytesIO(package.to_tar())) as archive:
        assert archive.extractfile("data/café.txt").read().decode("utf-8") == "crème brûlée\n"


def test_line_endings_are_preserved_byte_for_byte(make_payload: Callable[[], Payload]) -> None:
    lf, crlf = make_payload(), make_payload()
    lf["files"]["data/a.txt"] = "a\nb\n"
    crlf["files"]["data/a.txt"] = "a\r\nb\r\n"
    assert _package(lf).code_sha != _package(crlf).code_sha


def test_an_invalid_package_cannot_be_archived(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    payload["manifest"]["name"] = "Bad Name"
    with pytest.raises(ValidationError):
        _package(payload).to_tar()
    with pytest.raises(ValidationError):
        _ = _package(payload).code_sha


def test_a_bad_file_cannot_be_archived(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    payload["files"]["../escape.py"] = "x = 1\n"
    with pytest.raises(ValidationError):
        _package(payload).to_tar()


@pytest.mark.parametrize("pack", ["word_count", "csv_summary"])
def test_from_dir_and_from_submission_agree(pack: str, settings: Settings) -> None:
    from_dir = ToolPackage.from_dir(EXAMPLES / pack)
    files = {
        str(path.relative_to(EXAMPLES / pack)): path.read_text(encoding="utf-8")
        for path in sorted((EXAMPLES / pack).rglob("*"))
        if path.is_file() and path.name != "manifest.json" and "__pycache__" not in path.parts
    }
    manifest = json.loads((EXAMPLES / pack / "manifest.json").read_text(encoding="utf-8"))
    from_submission = ToolPackage.from_submission({"manifest": manifest, "files": files})
    assert from_dir.files == from_submission.files
    assert from_dir.manifest == from_submission.manifest
    assert from_dir.code_sha == from_submission.code_sha
    assert from_dir.validate(settings) == from_submission.validate(settings)


def test_a_written_package_reads_back_identically(
    tmp_path: Path, make_payload: Callable[[], Payload]
) -> None:
    package = _package(make_payload())
    written = package.to_dir(tmp_path / "out")
    assert ToolPackage.from_dir(written).code_sha == package.code_sha


GOLDEN_SHA = "aad23d2bcfc3bcae578c51963d1a1f900f8589d352d083d367deb51c4d8abece"
