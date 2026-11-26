"""`toolforge images build` (P14a): the command line, the refusals, the exit code and the digest line, with a stand-in for Docker."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from toolforge import cli, cli_images
from toolforge.config import RUNTIME_IMAGE
from toolforge.errors import ToolForgeError

REPO = Path(__file__).resolve().parent.parent
DIGEST = "sha256:" + "ab" * 32


def build(tmp_path: Path, tag: str = RUNTIME_IMAGE, **kw):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    calls: list[list[str]] = []
    out = io.StringIO()
    code = cli_images.build_image(
        tag,
        context=kw.pop("context", tmp_path),
        run=kw.pop("run", lambda argv: calls.append(argv) or 0),
        inspect=kw.pop("inspect", lambda t: DIGEST),
        which=kw.pop("which", lambda name: "/usr/bin/docker"),
        out=out,
        **kw,
    )
    return code, calls, out.getvalue()


def test_the_default_tag_is_the_runtime_image_and_the_command_is_an_argument_list(tmp_path: Path) -> None:
    code, calls, text = build(tmp_path)
    assert code == 0 and calls == [
        [
            "docker",
            "build",
            "--tag",
            "toolforge-runtime:py312",
            "--file",
            str(tmp_path / "Dockerfile"),
            str(tmp_path),
        ]
    ]
    assert "built toolforge-runtime:py312 (sha256:abababababab" in text and "toolforge doctor" in text


def test_no_cache_is_passed_on(tmp_path: Path) -> None:
    _, calls, _ = build(tmp_path, no_cache=True)
    assert "--no-cache" in calls[0] and calls[0][-1] == str(tmp_path)


@pytest.mark.parametrize("tag", ["--rm", "-x", "Upper:tag", "a b", "a;b", "", "x:" + "y" * 200, "$(id)"])
def test_a_tag_that_could_be_an_option_or_a_shell_string_is_refused_before_docker_runs(
    tmp_path: Path, tag: str
) -> None:
    ran: list[list[str]] = []
    with pytest.raises(ToolForgeError) as excinfo:
        build(tmp_path, tag, run=lambda argv: ran.append(argv) or 0)
    assert excinfo.value.code == "bad_image_tag" and ran == []


def test_a_folder_without_a_dockerfile_and_a_missing_docker_command_are_explained(tmp_path: Path) -> None:
    with pytest.raises(ToolForgeError) as excinfo:
        cli_images.build_image(context=tmp_path, which=lambda name: "/usr/bin/docker")
    assert excinfo.value.code == "no_dockerfile" and "checkout" in (excinfo.value.hint or "")
    with pytest.raises(ToolForgeError) as excinfo:
        build(tmp_path, which=lambda name: None)
    assert excinfo.value.code == "no_docker_cli"


def test_a_failed_build_returns_dockers_exit_code_and_says_it_needs_the_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, text = build(tmp_path, run=lambda argv: 3)
    assert code == 3 and text == ""
    assert "exit code 3" in capsys.readouterr().err


def test_an_unreadable_digest_is_said_not_invented(tmp_path: Path) -> None:
    _, _, text = build(tmp_path, inspect=lambda t: None)
    assert "digest not read" in text


def test_it_says_when_the_configured_image_is_a_different_one(tmp_path: Path) -> None:
    _, _, text = build(tmp_path, configured="python:3.12-slim")
    assert "TOOLFORGE_RUNTIME_IMAGE is python:3.12-slim" in text
    _, _, quiet = build(tmp_path, configured=RUNTIME_IMAGE)
    assert "note:" not in quiet


def test_the_checkout_holds_the_image_folder_the_command_builds_from() -> None:
    assert cli_images.default_context() == REPO / "images" / "runtime"
    assert (cli_images.default_context() / "Dockerfile").is_file()


def test_the_subcommand_is_registered_and_reports_an_error_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_images.shutil, "which", lambda name: None)
    assert cli.main(["images", "build", "--tag", "toolforge-runtime:test"]) == 1
    assert "no_docker_cli" in capsys.readouterr().err
    assert cli.main(["images", "build", "--tag=--rm"]) == 1
    assert "bad_image_tag" in capsys.readouterr().err
