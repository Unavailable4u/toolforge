"""P19: the published site loads nothing from anywhere else, sets no cookies and runs no tracking."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from site_helpers import REPO, SITE, build_dist

URL = re.compile(r"https?://[^\s\"'<>)\\]+")


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_dist(tmp_path_factory.mktemp("offline"))


def _allowed() -> list[str]:
    config = json.loads((SITE / "site.json").read_text(encoding="utf-8"))
    return [*config["allowed_external"], REPO]


def _scan_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        # Share-card metadata names the page's own public address; it is not something the page loads.
        text = re.sub(r"<meta\b[^>]*>", "", text)
    if path.suffix == ".svg":
        text = re.sub(r'\sxmlns="http://www\.w3\.org/2000/svg"', "", text)
    return text


def test_every_address_in_the_site_is_the_repository_or_on_the_list(dist: Path) -> None:
    allowed = _allowed()
    seen = 0
    for path in sorted(dist.rglob("*")):
        if path.suffix not in {".html", ".css", ".js", ".json", ".svg"}:
            continue
        for url in URL.findall(_scan_text(path)):
            seen += 1
            assert any(url.startswith(prefix) for prefix in allowed), f"{path.name} reaches {url}"
    assert seen > 0, "the scan found no address at all, so it is not scanning"


def test_the_allowed_list_is_short_and_has_no_trackers() -> None:
    allowed = _allowed()
    assert len(allowed) <= 6
    for url in allowed:
        assert url.startswith("https://")
        for tracker in ("google", "analytics", "facebook", "doubleclick", "cdn", "fonts"):
            assert tracker not in url


def test_the_page_never_pulls_in_another_origin(dist: Path) -> None:
    html = (dist / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<(?:script|link|img|iframe|source|video|audio|object|embed)\b[^>]*>", html):
        for attribute in re.findall(r'\b(?:src|href)="([^"]*)"', tag):
            assert not re.match(r"(?:https?:)?//", attribute), tag
    for banned in (
        "<iframe",
        "<form",
        "<object",
        "<embed",
        'rel="preconnect"',
        'rel="dns-prefetch"',
        'rel="prefetch"',
    ):
        assert banned not in html, banned


def test_the_stylesheet_loads_nothing(dist: Path) -> None:
    css = (dist / "styles.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "@font-face" not in css, "no web fonts: the page uses the system's"
    assert "url(" not in css, "a stylesheet that loads nothing has no url()"


def test_the_script_makes_no_request_and_sets_no_cookie(dist: Path) -> None:
    js = (dist / "app.js").read_text(encoding="utf-8")
    for banned in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "sendBeacon",
        "EventSource",
        "import(",
        "importScripts",
        "eval(",
        "new Function",
        "document.cookie",
        "innerHTML",
        "document.write",
    ):
        assert banned not in js, banned
    keys = re.findall(r"localStorage\.(?:get|set|remove)Item\(\s*(\w+|\"[^\"]+\")", js)
    assert keys and set(keys) == {"KEY"}, "the only thing stored is the theme choice, under one key"
    assert 'var KEY = "toolforge-theme"' in js


def test_the_policy_in_the_page_matches_what_the_files_do(dist: Path) -> None:
    html = (dist / "index.html").read_text(encoding="utf-8")
    policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html)
    assert policy
    directives = dict(part.strip().split(" ", 1) for part in policy[1].split(";") if part.strip())
    assert directives["default-src"] == "'self'"
    assert "unsafe-inline" not in policy[1] and "unsafe-eval" not in policy[1]
    assert directives["base-uri"] == "'none'"
    assert directives["form-action"] == "'none'"
    assert all(not re.search(r"https?:", value) for value in directives.values()), (
        "the policy opens no other origin"
    )


def test_links_are_relative_so_a_project_sub_path_works(dist: Path) -> None:
    html = (dist / "index.html").read_text(encoding="utf-8")
    for attribute in re.findall(r'\b(?:src|href)="([^"]*)"', html):
        assert not attribute.startswith("/"), (
            f"{attribute} is root-relative and would break under /repository-name/"
        )
    css = (dist / "styles.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css


def test_the_whole_site_fits_the_size_budget(dist: Path) -> None:
    total = sum(p.stat().st_size for p in dist.rglob("*") if p.is_file() and p.name != "social.png")
    assert total < 400 * 1024, f"{total // 1024} KB"
