"""P19: the built site in a real browser (headless Chromium through Playwright). Skipped when Playwright or its Chromium is missing.

pip install playwright && playwright install chromium && pytest -m browser
"""

from __future__ import annotations

import functools
import http.server
import re
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_helpers import REPO, build_dist  # noqa: E402

pytestmark = pytest.mark.browser

expect = pytest.importorskip("playwright.sync_api").expect


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    dist = build_dist(tmp_path_factory.mktemp("browser"))
    handler = functools.partial(_Quiet, directory=str(dist))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def new_page(browser: Any, base_url: str) -> Iterator[Any]:
    """`new_page(width=..., scheme=..., reduced_motion=..., javascript=...)` returns a page already on the site."""
    contexts: list[Any] = []

    def make(
        width: int = 1280,
        scheme: str = "light",
        reduced_motion: str = "no-preference",
        javascript: bool = True,
        permissions: list[str] | None = None,
        goto: bool = True,
    ) -> Any:
        context = browser.new_context(
            viewport={"width": width, "height": 900},
            color_scheme=scheme,
            reduced_motion=reduced_motion,
            java_script_enabled=javascript,
            permissions=permissions or [],
        )
        contexts.append(context)
        page = context.new_page()
        page.problems = []
        page.requests = []
        page.on("console", lambda m: page.problems.append(m.text) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: page.problems.append(str(e)))
        page.on("request", lambda r: page.requests.append(r.url))
        page.failed = []
        page.on("requestfailed", lambda r: page.failed.append(r.url))
        page.on("response", lambda r: page.failed.append(r.url) if r.status >= 400 else None)
        if goto:
            page.goto(base_url)
        return page

    yield make
    for context in contexts:
        context.close()


@pytest.mark.parametrize("width", [375, 768, 1280])
def test_it_loads_clean_makes_only_local_requests_and_never_scrolls_sideways(
    new_page: Any, base_url: str, width: int
) -> None:
    page = new_page(width=width)
    page.wait_for_load_state("networkidle")
    assert page.problems == []
    assert page.failed == [], page.failed
    assert page.requests and all(url.startswith(base_url) for url in page.requests), page.requests
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert page.locator("h1").count() == 1
    assert page.locator("section").count() == 15


def test_the_theme_toggle_switches_and_remembers(new_page: Any) -> None:
    page = new_page(scheme="light")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") is None
    light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.click("#theme-toggle")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != light_bg
    page.reload()
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark", (
        "the choice is kept"
    )
    page.click("#theme-toggle")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == light_bg


def test_the_system_dark_setting_is_followed_and_can_be_overridden(new_page: Any) -> None:
    page = new_page(scheme="dark")
    dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert dark_bg == "rgb(20, 22, 26)"
    page.click("#theme-toggle")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(246, 243, 238)"


def test_the_menu_collapses_on_a_phone_and_works_from_the_keyboard(new_page: Any) -> None:
    page = new_page(width=375)
    nav, toggle = page.locator("#site-nav"), page.locator("#menu-toggle")
    assert toggle.is_visible()
    assert not nav.is_visible()
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert nav.is_visible()
    page.keyboard.press("Escape")
    assert toggle.get_attribute("aria-expanded") == "false"
    assert not nav.is_visible()
    assert page.evaluate("document.activeElement.id") == "menu-toggle"
    toggle.click()
    page.click("#site-nav a[href='#gates']")
    assert toggle.get_attribute("aria-expanded") == "false", "choosing a section closes the menu"


def test_on_a_wide_screen_the_menu_button_is_not_shown(new_page: Any) -> None:
    page = new_page(width=1280)
    assert not page.locator("#menu-toggle").is_visible()
    assert page.locator("#site-nav").is_visible()


def test_the_first_tab_stop_is_the_skip_link_and_it_works(new_page: Any) -> None:
    page = new_page()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "skip"
    assert page.evaluate("document.activeElement.textContent") == "Skip to content"
    page.keyboard.press("Enter")
    assert page.evaluate("location.hash") == "#main"


def test_the_github_button_is_a_labelled_new_tab_link_with_a_visible_focus_ring(new_page: Any) -> None:
    page = new_page()
    link = page.locator("header a.gh")
    assert link.get_attribute("href") == REPO
    assert link.get_attribute("target") == "_blank"
    assert link.get_attribute("aria-label") == "ToolForge on GitHub"
    link.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    assert page.evaluate("parseFloat(getComputedStyle(document.activeElement).outlineWidth)") >= 2


@pytest.mark.parametrize(
    "target", ["idea", "tour", "architecture", "gates", "features", "security", "proof", "quickstart"]
)
def test_each_menu_link_lands_with_the_section_below_the_sticky_header(new_page: Any, target: str) -> None:
    page = new_page(reduced_motion="reduce")
    page.click(f"#site-nav a[href='#{target}']")
    expect(page).to_have_url(re.compile(rf"#{target}$"))
    top = page.evaluate(f"document.getElementById('{target}').getBoundingClientRect().top")
    header = page.evaluate("document.querySelector('.site-header').getBoundingClientRect().height")
    assert header - 2 <= top <= header + 100, f"{target} lands at {top}px under a {header}px header"


def test_the_copy_button_copies_the_commands(new_page: Any) -> None:
    page = new_page(permissions=["clipboard-read", "clipboard-write"])
    button = page.locator("#quickstart button.copy").first
    button.scroll_into_view_if_needed()
    button.click()
    expect(button).to_have_text("Copied")
    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied.splitlines()[0].startswith("git clone --recurse-submodules https://github.com/")
    assert "pip install -e" in copied
    expect(button).to_have_text("Copy", timeout=5000)


def test_motion_stops_when_the_visitor_asks_for_less(new_page: Any) -> None:
    moving = new_page(reduced_motion="no-preference")
    assert moving.evaluate("getComputedStyle(document.querySelector('.step-1')).animationName") != "none"
    calm = new_page(reduced_motion="reduce")
    assert calm.evaluate("getComputedStyle(document.querySelector('.step-1')).animationName") == "none"
    assert calm.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto"


def test_everything_can_be_read_without_javascript(new_page: Any) -> None:
    page = new_page(javascript=False)
    assert page.locator("h1").inner_text().startswith("Let an AI agent build its own tools")
    assert page.locator("#site-nav").is_visible()
    assert not page.locator("#menu-toggle").is_visible()
    assert not page.locator("#theme-toggle").is_visible()
    assert not page.locator("button.copy").first.is_visible()
    assert page.locator("#qs-1").inner_text().startswith("git clone")
    assert page.locator("#gate-g1").inner_text()
    page.locator("#faq details").first.locator("summary").click()
    assert page.locator("#faq details").first.get_attribute("open") is not None


def test_a_wide_table_scrolls_inside_its_box_not_the_page(new_page: Any) -> None:
    page = new_page(width=375)
    wrap = page.locator("#tools .table-wrap")
    assert wrap.evaluate("el => el.scrollWidth > el.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    wrap.focus()
    assert page.evaluate("document.activeElement.classList.contains('table-wrap')")


def test_the_evidence_section_says_not_measured_instead_of_showing_a_number(new_page: Any) -> None:
    page = new_page()
    text = page.locator("#proof").inner_text()
    assert "Not measured yet" in text
    assert "%" not in text
    assert (
        page.locator("#proof .nm")
        .first.evaluate("el => getComputedStyle(el).backgroundImage")
        .startswith("repeating-linear-gradient")
    )


@pytest.mark.parametrize("width", [375, 1280])
def test_the_github_button_is_visible_and_reachable_by_keyboard(new_page: Any, width: int) -> None:
    page = new_page(width=width)
    link = page.locator("header a.gh")
    assert link.is_visible()
    for _ in range(20):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.classList.contains('gh')"):
            break
    else:
        pytest.fail("Tab never reached the GitHub button")
