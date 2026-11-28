"""P19: the static site builds, deterministically, from the sources that own each list; the repository address is never typed in."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from site_helpers import REPO, ROOT, SITE, build_dist, parse, script

from toolforge.attacks.catalog import ATTACKS
from toolforge.config import ENV_PREFIX, Settings
from toolforge.server.tools import META_TOOLS
from toolforge.verify.pipeline import GATE_INFO

bs = script("build_site")


# --- The repository address -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://github.com/acme/toolforge", "https://github.com/acme/toolforge"),
        ("https://github.com/acme/toolforge.git", "https://github.com/acme/toolforge"),
        ("https://github.com/acme/toolforge/", "https://github.com/acme/toolforge"),
        ("git@github.com:acme/toolforge.git", "https://github.com/acme/toolforge"),
        ("ssh://git@github.com/acme/toolforge.git", "https://github.com/acme/toolforge"),
        ("https://x-access-token:SECRET@github.com/acme/toolforge.git", "https://github.com/acme/toolforge"),
        ("http://github.com/acme/tool.forge", "https://github.com/acme/tool.forge"),
    ],
)
def test_a_github_remote_becomes_the_https_address_without_credentials(given: str, expected: str) -> None:
    url = bs.normalise_repo_url(given)
    assert url == expected
    assert "SECRET" not in url


@pytest.mark.parametrize(
    "bad",
    ["https://gitlab.com/acme/toolforge", "git@example.com:a/b.git", "not a url", "https://github.com/acme"],
)
def test_an_address_that_is_not_github_is_an_error(bad: str) -> None:
    with pytest.raises(bs.SiteError):
        bs.normalise_repo_url(bad)


def test_the_address_comes_from_the_documented_sources_in_order() -> None:
    env = {"GITHUB_REPOSITORY": "from-env/toolforge"}

    def remote() -> str:
        return "git@github.com:from-git/toolforge.git"

    assert bs.resolve_repo_url("https://github.com/cli/x", "https://github.com/site/x", env, remote) == (
        "https://github.com/cli/x",
        "--repo-url",
    )
    assert bs.resolve_repo_url(None, "https://github.com/site/x", env, remote) == (
        "https://github.com/site/x",
        "site/site.json",
    )
    assert bs.resolve_repo_url(None, "", env, remote) == (
        "https://github.com/from-env/toolforge",
        "GITHUB_REPOSITORY",
    )
    assert bs.resolve_repo_url(None, "", {}, remote) == (
        "https://github.com/from-git/toolforge",
        "git remote origin",
    )
    url, source = bs.resolve_repo_url(None, "", {}, lambda: None)
    assert bs.is_placeholder(url)
    assert "placeholder" in source


def test_a_present_but_wrong_source_is_an_error_not_a_fall_through() -> None:
    with pytest.raises(bs.SiteError):
        bs.resolve_repo_url(None, "https://gitlab.com/a/b", {}, lambda: "git@github.com:ok/ok.git")
    with pytest.raises(bs.SiteError):
        bs.resolve_repo_url(None, "", {"GITHUB_REPOSITORY": "no-slash"}, lambda: "git@github.com:ok/ok.git")
    with pytest.raises(bs.SiteError):
        bs.resolve_repo_url(None, "", {}, lambda: "https://gitlab.com/a/b.git")


def test_the_committed_site_json_does_not_hard_code_an_address() -> None:
    config = json.loads((SITE / "site.json").read_text(encoding="utf-8"))
    assert config["repo_url"] == ""


def test_a_release_build_refuses_a_placeholder_address(tmp_path: Path) -> None:
    with pytest.raises(bs.SiteError, match="refusing a release build"):
        bs.build(tmp_path / "d", release=True, environ={}, git_remote=lambda: None, social=False)
    # A preview with the same missing address builds and warns.
    info = bs.build(tmp_path / "p", environ={}, git_remote=lambda: None, social=False)
    assert any("placeholder" in w for w in info.warnings)
    # A real address, from the environment, lets the release build through.
    info = bs.build(
        tmp_path / "r",
        release=True,
        environ={"GITHUB_REPOSITORY": "acme/toolforge"},
        git_remote=lambda: None,
        social=False,
    )
    assert info.repo_url == "https://github.com/acme/toolforge"
    assert info.repo_source == "GITHUB_REPOSITORY"


def test_the_repository_address_reaches_the_button_and_the_footer(tmp_path: Path) -> None:
    page = parse(build_dist(tmp_path) / "index.html")
    hrefs = [a["href"] for a in page.links]
    assert hrefs.count(REPO) >= 2
    assert not any("OWNER" in h for h in hrefs)


# --- The build itself -----------------------------------------------------------------------------------------------------------


def test_the_build_writes_the_expected_files_and_leaves_no_placeholder(tmp_path: Path) -> None:
    dist = build_dist(tmp_path)
    names = sorted(p.relative_to(dist).as_posix() for p in dist.rglob("*") if p.is_file())
    assert names == ["app.js", "assets/favicon.svg", "data/results.json", "index.html", "styles.css"]
    for path in dist.rglob("*"):
        if path.is_file():
            assert "{{" not in path.read_text(encoding="utf-8"), path


def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    a, b = build_dist(tmp_path, name="a"), build_dist(tmp_path, name="b")
    files = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    assert files
    for name in files:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
        assert b"\r" not in (a / name).read_bytes(), f"{name} has CRLF line endings"


def test_the_site_is_small(tmp_path: Path) -> None:
    dist = build_dist(tmp_path)
    total = sum(p.stat().st_size for p in dist.rglob("*") if p.is_file())
    assert total < 400 * 1024, f"the site weighs {total // 1024} KB"
    assert (dist / "styles.css").stat().st_size < 40 * 1024
    assert (dist / "app.js").stat().st_size < 12 * 1024


def test_an_unknown_placeholder_or_partial_stops_the_build(tmp_path: Path) -> None:
    site = tmp_path / "site"
    shutil.copytree(SITE, site, ignore=shutil.ignore_patterns("dist"))
    index = site / "src" / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace("{{title}}", "{{no_such_thing}}"), encoding="utf-8"
    )
    with pytest.raises(bs.SiteError, match="unknown placeholder"):
        bs.build(tmp_path / "d1", site=site, repo_url=REPO, environ={}, social=False)
    index.write_text(
        (SITE / "src" / "index.html")
        .read_text(encoding="utf-8")
        .replace("{{partial:hero}}", "{{partial:ghost}}"),
        encoding="utf-8",
    )
    with pytest.raises(bs.SiteError, match="does not exist"):
        bs.build(tmp_path / "d2", site=site, repo_url=REPO, environ={}, social=False)


def test_the_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert bs.main(["--dist", str(tmp_path / "ok"), "--repo-url", REPO]) == 0
    assert "repository address" in capsys.readouterr().out
    assert bs.main(["--dist", str(tmp_path / "bad"), "--repo-url", "https://example.com/a/b"]) == 1
    assert "not a GitHub repository address" in capsys.readouterr().err


# --- Every generated list comes from the code that owns it ----------------------------------------------------------------------


def test_the_tools_table_lists_exactly_the_servers_tools(tmp_path: Path) -> None:
    text = (build_dist(tmp_path) / "index.html").read_text(encoding="utf-8")
    table = text[text.index('id="tools"') : text.index('id="quickstart"')]
    shown = re.findall(r'<th scope="row"><code>(forge_[a-z_]+)</code>', table)
    assert shown == [tool.name for tool in META_TOOLS]


def test_the_gate_cards_are_the_gate_info(tmp_path: Path) -> None:
    page = parse(build_dist(tmp_path) / "index.html")
    ids = [i for i in page.ids if i.startswith("gate-")]
    assert ids == [f"gate-{g.id.lower()}" for g in GATE_INFO]
    assert len(GATE_INFO) == 7, (
        "the page says 'seven gates'; change the copy in partials/gates.html with the code"
    )
    assert "seven gates" in (SITE / "src" / "partials" / "gates.html").read_text(encoding="utf-8").lower()
    for gate in GATE_INFO:
        assert (SITE / "src" / "assets" / "gates" / f"{gate.id.lower()}.svg").is_file()


def test_the_attack_table_lists_every_attack_once(tmp_path: Path) -> None:
    text = (build_dist(tmp_path) / "index.html").read_text(encoding="utf-8")
    shown = re.findall(r'<th scope="row"><code>([a-z_]+)</code><br>', text)
    assert shown == [a.id for a in ATTACKS]


def test_the_settings_table_lists_every_setting(tmp_path: Path) -> None:
    text = (build_dist(tmp_path) / "index.html").read_text(encoding="utf-8")
    shown = set(re.findall(r"<code>(TOOLFORGE_[A-Z0-9_]+)</code>", text[text.index('class="settings"') :]))
    assert {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields} <= shown


def test_the_limitations_are_the_limitations_file(tmp_path: Path) -> None:
    source = (ROOT / "docs" / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    expected = len(re.findall(r"^\* \*\*", source, re.M))
    text = (build_dist(tmp_path) / "index.html").read_text(encoding="utf-8")
    block = text[text.index('id="limits"') : text.index('id="faq"')]
    assert expected >= 5
    assert block.count("<li>") == expected


# --- The claims on the page are backed ------------------------------------------------------------------------------------------


def test_status_chips_agree_with_progress_md() -> None:
    config = json.loads((SITE / "site.json").read_text(encoding="utf-8"))
    assert bs.check_claims(config, bs.progress_done()) == []


def test_a_chip_without_a_finished_patch_is_caught() -> None:
    config = {
        "capabilities": [{"name": "A", "chip": "built", "patches": ["P99"]}],
        "features": [
            {"name": "B", "chip": "built", "patches": ["P20"]},
            {"name": "C", "chip": "experimental", "patches": []},
            {"name": "D", "chip": "planned", "patches": ["P20"]},
            {"name": "E", "chip": "shipped", "patches": ["P01"]},
        ],
    }
    done = {"P01": True, "P20": False}
    problems = bs.check_claims(config, done)
    assert any("P99 is not in PROGRESS.md" in p for p in problems)
    assert any("P20 is not done" in p and "'B'" in p for p in problems)
    assert any("names no patch" in p and "'C'" in p for p in problems)
    assert any("chip must be one of" in p and "'E'" in p for p in problems)
    assert not any("'D'" in p for p in problems)


def test_desktop_guis_are_never_claimed() -> None:
    config = json.loads((SITE / "site.json").read_text(encoding="utf-8"))
    desktop = next(row for row in config["capabilities"] if "Desktop" in row["name"])
    assert desktop["chip"] == "planned"
    assert next(row for row in config["features"] if "tour" in row["name"].lower())["chip"] == "planned"


def test_every_quick_start_command_is_in_the_document_it_cites() -> None:
    config = json.loads((SITE / "site.json").read_text(encoding="utf-8"))
    for group in config["quickstart"]:
        source = (ROOT / group["source"]).read_text(encoding="utf-8")
        for command in group["commands"]:
            wanted = command.replace("{{repo_url}}", "<this repository>")
            assert wanted in source, f"{group['title']}: {command!r} is not in {group['source']}"


def test_every_feature_link_points_at_a_file_that_exists() -> None:
    config = json.loads((SITE / "site.json").read_text(encoding="utf-8"))
    for row in config["features"]:
        if row["doc"]:
            assert (ROOT / row["doc"]).is_file(), row["doc"]


def test_the_pages_workflow_is_pinned_and_least_privilege() -> None:
    text = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a commit"
    assert "build_site.py --release" in text
    assert "pull_request_target" not in text
    top = text.split("jobs:")[0]
    assert "contents: read" in top
    assert "pages: write" not in top, "the Pages write permission belongs to the deploy job only"
    assert "id-token: write" in text


def test_a_missing_results_file_still_builds_and_says_not_measured(tmp_path: Path) -> None:
    script("build_site").build(
        tmp_path / "d",
        repo_url=REPO,
        results_path=tmp_path / "does-not-exist.json",
        environ={},
        git_remote=lambda: None,
        social=False,
    )
    text = (tmp_path / "d" / "index.html").read_text(encoding="utf-8")
    assert "Not measured yet" in text
    assert 'class="fig"' not in text
    assert json.loads((tmp_path / "d" / "data" / "results.json").read_text(encoding="utf-8")) == {}


# --- The social preview image ---------------------------------------------------------------------------------------------------


def _fake_converter(directory: Path) -> None:
    program = directory / "rsvg-convert"
    program.write_text('#!/bin/sh\nfor last; do :; done\nprintf "PNG" > "$last"\n', encoding="utf-8")
    program.chmod(0o755)


def test_with_a_converter_the_page_gets_a_share_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_converter(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    info = bs.build(
        tmp_path / "d", repo_url="https://github.com/acme/toolforge", environ={}, git_remote=lambda: None
    )
    assert info.social
    assert (tmp_path / "d" / "assets" / "social.png").read_bytes() == b"PNG"
    html = (tmp_path / "d" / "index.html").read_text(encoding="utf-8")
    assert '<meta property="og:image" content="https://acme.github.io/toolforge/assets/social.png">' in html
    assert 'content="summary_large_image"' in html
    assert 'property="og:image:width" content="1200"' in html


def test_without_a_converter_the_site_builds_with_a_plain_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bs, "render_social", lambda out: None)
    info = bs.build(tmp_path / "d", repo_url=REPO, environ={}, git_remote=lambda: None)
    assert not info.social
    assert any("no social image" in w for w in info.warnings)
    assert not (tmp_path / "d" / "assets" / "social.png").exists()
    html = (tmp_path / "d" / "index.html").read_text(encoding="utf-8")
    assert "og:image" not in html
    assert 'content="summary"' in html
    assert 'property="og:title"' in html
