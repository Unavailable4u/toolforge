"""The page: no way for server data to become markup or script, and the files are in the package."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pytest
from dashboard_helpers import make_dashboard

from toolforge.registry import Registry

STATIC = Path(str(resources.files("toolforge.dashboard") / "static"))
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")

# Every way this file could turn a string into markup, a script or a navigation.
FORBIDDEN = [
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "createContextualFragment",
    "DOMParser",
    "eval(",
    "new Function",
    'setTimeout("',
    "setTimeout('",
    'setInterval("',
    "importScripts",
    "location.href =",
    "location.assign",
    "localStorage",
    "document.cookie",
    "XMLHttpRequest",
    "postMessage",
    "window.open",
]


def code_only(text: str) -> str:
    """The script without // comments (the file's own header names the sinks it avoids)."""
    return "\n".join(re.sub(r"^\s*//.*$", "", line) for line in text.splitlines())


@pytest.mark.parametrize("sink", FORBIDDEN)
def test_the_script_has_no_html_or_code_sink(sink: str) -> None:
    assert sink not in code_only(JS)


def test_the_only_markup_sink_is_a_sandboxed_frame() -> None:
    code = code_only(JS)
    assert code.count(".srcdoc") == 1
    assert "sandbox: ''" in code  # an empty sandbox attribute: no scripts, no same-origin
    assert re.search(r"allow-(scripts|same-origin|top-navigation|popups|forms)", code) is None


def test_the_page_has_no_inline_script_handler_or_remote_resource() -> None:
    lowered = HTML.lower()
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", lowered) == []  # every script tag has a src
    assert re.search(r"\son[a-z]+\s*=", lowered) is None  # no onclick= and friends
    assert "javascript:" not in lowered and "<style" not in lowered and "style=" not in lowered
    assert re.findall(r"(?:src|href)=\"(https?:)?//", HTML) == []
    assert 'src="/static/app.js"' in HTML and 'href="/static/style.css"' in HTML


def test_the_script_does_not_use_server_data_in_an_href_it_did_not_encode() -> None:
    for match in re.finditer(r"link\([^)]*\)|href: ?[^,}]+", code_only(JS)):
        text = match.group(0)
        if "'#/" in text:
            assert "enc(" in text or "'#/approvals/' +" in text or "'#/tools'" in text, text


def test_a_hash_is_the_only_thing_the_script_navigates_with() -> None:
    assert "location.hash" in JS and "javascript:" not in JS


def test_the_page_files_are_packaged_and_the_server_returns_them(tmp_path) -> None:
    with Registry(tmp_path / "home") as registry:
        _, client, _ = make_dashboard(tmp_path, registry)
        for name, kind in (("app.js", "javascript"), ("style.css", "css")):
            res = client.get(f"/static/{name}")
            assert (
                res.status_code == 200
                and kind in res.headers["content-type"]
                and res.text == (STATIC / name).read_text()
            )
        page = client.get("/")
        assert (
            page.status_code == 200
            and page.headers["content-type"].startswith("text/html")
            and "ToolForge" in page.text
        )


def test_pyproject_ships_the_static_files() -> None:
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text()
    assert '"toolforge.dashboard" = ["static/*"]' in text and "fastapi==" in text


def test_nested_lists_of_nodes_are_flattened_all_the_way() -> None:
    # A view that returns a list of lists must not reach the page as "[object HTMLParagraphElement]". Found by driving the page in jsdom.
    code = code_only(JS)
    assert "kids.flat(Infinity)" in code and "nodes.flat(Infinity)" in code
    assert ".flat()" not in code
