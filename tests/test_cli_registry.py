"""`toolforge registry list` and `toolforge registry show`."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from toolforge.cli import main
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord

Make = Callable[..., ToolPackage]


@pytest.fixture
def registry(tmp_home: Path) -> Iterator[Registry]:
    """Overrides the shared fixture: a registry in the directory the CLI opens through TOOLFORGE_HOME."""
    with Registry(tmp_home) as registry:
        yield registry


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


def test_list_on_an_empty_home_prints_an_empty_table(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["registry", "list"]) == 0
    out = _out(capsys)
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "VERSION", "STATUS", "USES", "SUCCESS", "RATE", "CREATED"]
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert out.strip().endswith("0 versions")
    assert (tmp_home / "registry.sqlite3").is_file()  # the home was initialised


def test_list_on_an_empty_home_as_json(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["registry", "list", "--json"]) == 0
    assert json.loads(_out(capsys)) == {"versions": []}


def test_list_shows_versions_with_status_and_honest_statistics(
    registry: Registry,
    make_package: Make,
    activate: Callable[[VersionRecord], VersionRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    active = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    registry.add_version(make_package("word_count", "1.1.0"), created_by="agent")
    registry.add_version(make_package("pdf_text", "0.1.0"), created_by="agent")
    for n in range(10):
        registry.record_call(active.id, ok=n < 8, duration_ms=4)
    assert main(["registry", "list"]) == 0
    out = _out(capsys)
    rows = [line.split() for line in out.splitlines()[2:] if line and not line.endswith("versions")]
    assert [row[:4] for row in rows] == [
        ["pdf_text", "0.1.0", "submitted", "0"],
        ["word_count", "1.0.0", "active", "10"],
        ["word_count", "1.1.0", "submitted", "0"],
    ]
    assert "80% of last 10" in out and out.count("not measured") == 2  # never a made-up 0%
    assert out.strip().endswith("3 versions")


def test_list_json(registry: Registry, make_package: Make, capsys: pytest.CaptureFixture[str]) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    assert main(["registry", "list", "--json"]) == 0
    (entry,) = json.loads(_out(capsys))["versions"]
    assert (
        entry["name"] == "word_count"
        and entry["status"] == "submitted"
        and entry["code_sha"] == record.code_sha
    )
    assert entry["uses"] == 0 and entry["success_rate"] is None and entry["blob_sha"] is None


def test_show_a_tool_lists_its_versions(
    registry: Registry,
    make_package: Make,
    activate: Callable[[VersionRecord], VersionRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    registry.add_version(make_package("word_count", "1.1.0"), created_by="agent")
    assert main(["registry", "show", "word_count"]) == 0
    out = _out(capsys)
    assert "tool: word_count" in out and "active version: 1.0.0" in out
    assert "1.1.0" in out and "registry show word_count@<version>" in out


def test_show_a_tool_with_no_active_version(
    registry: Registry, make_package: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.add_version(make_package(), created_by="agent")
    assert main(["registry", "show", "word_count"]) == 0
    assert "active version: none" in _out(capsys)


def test_show_a_version_in_detail(
    registry: Registry, make_package: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    package = make_package(
        "fetch_page",
        "1.0.0",
        description="Fetch a web page and return its title.",
        network=["example.com"],
        secrets=["site_key"],
    )
    record = registry.add_version(package, created_by="builder")
    registry.record_gate(record.id, "G1", "passed")
    registry.record_gate(record.id, "G5", "skipped", {"why": "no model configured"})
    approval = registry.add_approval(record.id, {"network": ["example.com"]})
    assert main(["registry", "show", "fetch_page@1.0.0"]) == 0
    out = _out(capsys)
    assert "fetch_page 1.0.0  [submitted]" in out
    assert "Fetch a web page and return its title." in out
    assert "network: example.com; secrets: site_key" in out
    assert record.code_sha in out and "not frozen yet" in out and "by builder" in out
    assert "success rate not measured (needs 5 calls)" in out
    assert "G1" in out and "passed" in out and "G5" in out and "skipped" in out
    assert (
        "no model configured" not in out
    )  # gate detail can hold sandbox text; the text view shows outcomes only
    assert str(approval.id) in out and "pending" in out


def test_show_a_version_as_json_includes_detail(
    registry: Registry, make_package: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    registry.record_gate(record.id, "G3", "failed", {"hints": ["handle empty input"]})
    assert main(["registry", "show", "word_count@1.0.0", "--json"]) == 0
    data = json.loads(_out(capsys))
    assert data["manifest"]["name"] == "word_count" and data["status"] == "submitted"
    assert data["gates"][0]["detail"] == {"hints": ["handle empty input"]}
    assert data["approvals"] == [] and data["success_rate"] is None


def test_show_unknowns_exit_1_with_a_hint(
    tmp_home: Path, registry: Registry, make_package: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.add_version(make_package(), created_by="agent")
    for target, code in (
        ("ghost", "unknown_tool"),
        ("word_count@9.9.9", "unknown_version"),
        ("ghost@1.0.0", "unknown_version"),
    ):
        assert main(["registry", "show", target]) == 1
        err = capsys.readouterr().err
        assert f"error [{code}]" in err and "hint: see what exists with `toolforge registry list`" in err


@pytest.mark.parametrize("target", ["@1.0.0", "word_count@", "@", ""])
def test_show_malformed_targets_exit_1(
    tmp_home: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    assert main(["registry", "show", target]) == 1
    assert "error [invalid_target]" in capsys.readouterr().err


def test_hostile_text_cannot_hijack_the_terminal(
    registry: Registry, make_package: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    # The manifest rules forbid control characters, so plant them directly in the row to prove the CLI does not trust the database.
    manifest = dict(
        record.manifest, description="Count words.\x1b[2J\x1b[31m hidden \x07 text of enough length"
    )
    with registry.db.transaction() as conn:
        conn.execute(
            "UPDATE versions SET manifest_json = ?, created_by = ? WHERE id = ?",
            (json.dumps(manifest), "evil\x1b[0m", record.id),
        )
    assert main(["registry", "show", "word_count@1.0.0"]) == 0
    assert "\x1b" not in _out(capsys)


def test_registry_without_a_subcommand_prints_help(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["registry"]) == 0
    out = _out(capsys)
    assert "list" in out and "show" in out


def test_the_installed_command_works_end_to_end(tmp_home: Path) -> None:
    """Through a real process: the entry point, the settings and the home directory."""
    env = {"TOOLFORGE_HOME": str(tmp_home), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-I", "-m", "toolforge.cli", "registry", "list"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("0 versions")
