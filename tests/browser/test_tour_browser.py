"""P20: the guided tour in a real browser (headless Chromium through Playwright), under the page's own Content-Security-Policy.

pip install playwright && playwright install chromium && pytest -m browser

Play is driven by Playwright's page clock, so no test waits for a timer.
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_helpers import REPO, SITE, build_dist  # noqa: E402

pytestmark = pytest.mark.browser

expect = pytest.importorskip("playwright.sync_api").expect

CAPTIONS = json.loads((SITE / "src" / "tour_captions.json").read_text(encoding="utf-8"))
DATA = json.loads((SITE / "data" / "tour.json").read_text(encoding="utf-8"))
STEPS = [step["id"] for step in DATA["steps"]]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    dist = build_dist(tmp_path_factory.mktemp("tour-browser"))
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
def open_tour(browser: Any, base_url: str) -> Iterator[Any]:
    """A page on the site whose tour has loaded (unless `live=False`), with console errors and requests collected."""
    contexts: list[Any] = []

    def make(
        *,
        width: int = 1280,
        reduced_motion: str = "reduce",
        javascript: bool = True,
        clock: bool = False,
        setup: Any = None,
        live: bool = True,
        permissions: list[str] | None = None,
    ) -> Any:
        context = browser.new_context(
            viewport={"width": width, "height": 900},
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
        if clock:
            page.clock.install()
        if setup:
            setup(page)
        page.goto(base_url)
        if live and javascript:
            expect(page.locator("#tour-app")).to_be_visible()
        return page

    yield make
    for context in contexts:
        context.close()


def _count(page: Any) -> Any:
    return page.locator("#tour-count")


def _at(page: Any, number: int) -> None:
    expect(_count(page)).to_have_text(f"Step {number} of {len(STEPS)}")


def _shown(text: str) -> str:
    glossary = json.loads((SITE / "src" / "glossary.json").read_text(encoding="utf-8"))
    text = re.sub(r"\[\[([a-z_]+)(?:\|([^\]]+))?\]\]", lambda m: m[2] or glossary[m[1]]["term"], text)
    return text.replace("`", "")


def _active(page: Any) -> str:
    return page.evaluate("document.activeElement.id")


def test_the_interactive_tour_replaces_the_list_without_errors(open_tour: Any, base_url: str) -> None:
    page = open_tour()
    assert not page.locator(".tour-static").is_visible()
    assert not page.locator("#tour-failed").is_visible()
    _at(page, 1)
    assert page.problems == []
    assert all(url.startswith(base_url) for url in page.requests), page.requests
    assert page.context.cookies() == []


@pytest.mark.parametrize("mode", ["simple", "technical"])
def test_every_step_in_both_modes_with_the_keyboard_only(open_tour: Any, mode: str) -> None:
    page = open_tour()
    page.locator(f"button[data-mode='{mode}']").focus()
    page.keyboard.press("Enter")
    for number, step in enumerate(DATA["steps"], start=1):
        _at(page, number)
        caption = CAPTIONS[step["id"]]
        expect(page.locator("#tour-step-title")).to_have_text(caption["title"])
        expect(page.locator("#tour-caption")).to_have_text(_shown(caption[mode]))
        expect(page.locator("#tour-note")).to_have_text(step["note"])
        lines = page.locator("#tour-ledger .tl-line")
        expect(lines).to_have_count(len(step["events"]))
        if not step["events"]:
            expect(page.locator("#tour-ledger")).to_have_text("No event recorded for this step.")
        expect(page.locator("#tour-progress [aria-current='step']")).to_have_text(str(number))
        page.keyboard.press("ArrowRight")
    _at(page, len(STEPS))
    assert page.problems == []


def test_arrow_keys_home_and_end_move_between_steps(open_tour: Any) -> None:
    page = open_tour()
    page.locator("#tour-next").focus()
    page.keyboard.press("ArrowLeft")
    _at(page, 1)
    page.keyboard.press("ArrowRight")
    _at(page, 2)
    page.keyboard.press("End")
    _at(page, len(STEPS))
    assert page.locator("#tour-next").get_attribute("aria-disabled") == "true"
    page.keyboard.press("ArrowRight")
    _at(page, len(STEPS))
    page.keyboard.press("ArrowLeft")
    _at(page, len(STEPS) - 1)
    page.keyboard.press("Home")
    _at(page, 1)
    assert _active(page) == "tour-next", "the keys never move focus"


def test_keys_outside_the_tour_or_inside_a_scrolling_block_are_left_alone(open_tour: Any) -> None:
    page = open_tour()
    page.locator("#theme-toggle").focus()
    page.keyboard.press("ArrowRight")
    _at(page, 1)
    page.locator("#tour-ledger").focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("End")
    _at(page, 1)


def test_back_next_and_the_numbers_work_with_the_pointer(open_tour: Any) -> None:
    page = open_tour()
    page.click("#tour-next")
    page.click("#tour-next")
    _at(page, 3)
    page.click("#tour-back")
    _at(page, 2)
    page.click("#tour-progress button[data-step='7']")
    _at(page, 8)


def test_the_toggle_swaps_the_text_and_remembers_the_choice(open_tour: Any) -> None:
    page = open_tour()
    caption = page.locator("#tour-caption")
    expect(caption).to_have_text(_shown(CAPTIONS["meet"]["simple"]))
    page.get_by_role("button", name="Technical").click()
    expect(caption).to_have_text(_shown(CAPTIONS["meet"]["technical"]))
    assert page.locator("button[data-mode='technical']").get_attribute("aria-pressed") == "true"
    assert page.locator("button[data-mode='simple']").get_attribute("aria-pressed") == "false"
    page.reload()
    expect(page.locator("#tour-app")).to_be_visible()
    expect(caption).to_have_text(_shown(CAPTIONS["meet"]["technical"]))


def test_blocked_storage_falls_back_to_simple_and_still_switches(open_tour: Any) -> None:
    def block(page: Any) -> None:
        page.add_init_script(
            "Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked'); }});"
        )

    page = open_tour(setup=block)
    expect(page.locator("#tour-caption")).to_have_text(_shown(CAPTIONS["meet"]["simple"]))
    page.get_by_role("button", name="Technical").click()
    expect(page.locator("#tour-caption")).to_have_text(_shown(CAPTIONS["meet"]["technical"]))
    assert not [p for p in page.problems if "blocked" in p]


def test_the_live_region_updates_with_the_step(open_tour: Any) -> None:
    page = open_tour()
    region = page.locator("#tour [aria-live='polite']")
    expect(region).to_contain_text(CAPTIONS["meet"]["title"])
    page.click("#tour-next")
    expect(region).to_contain_text(CAPTIONS["find"]["title"])
    expect(region).to_contain_text(DATA["steps"][1]["note"])


def test_play_is_off_until_asked_then_advances_every_five_seconds(open_tour: Any) -> None:
    page = open_tour(clock=True)
    page.clock.run_for(20_000)
    _at(page, 1)
    page.click("#tour-play")
    expect(page.locator("#tour-play")).to_have_text("Pause")
    page.mouse.move(0, 0)
    page.clock.run_for(4_900)
    _at(page, 1)
    page.clock.run_for(200)
    _at(page, 2)
    page.clock.run_for(5_000)
    _at(page, 3)
    page.click("#tour-play")
    page.clock.run_for(20_000)
    _at(page, 3)


def test_play_waits_while_focus_or_the_pointer_is_on_the_text(open_tour: Any) -> None:
    page = open_tour(clock=True)
    page.click("#tour-play")
    page.mouse.move(0, 0)
    page.clock.run_for(5_100)
    _at(page, 2)
    page.locator("#tour-ledger").focus()
    expect(page.locator("#tour-held")).to_be_visible()
    page.clock.run_for(30_000)
    _at(page, 2)
    page.locator("#tour-ledger").hover()
    page.mouse.move(0, 0)  # the pointer leaving does not release a focused area
    page.clock.run_for(30_000)
    _at(page, 2)
    page.locator("#tour-play").focus()
    expect(page.locator("#tour-held")).to_be_hidden()
    page.clock.run_for(5_100)
    _at(page, 3)
    page.locator("#tour-caption").hover()
    page.clock.run_for(30_000)
    _at(page, 3)
    page.mouse.move(0, 0)
    page.clock.run_for(5_100)
    _at(page, 4)


def test_play_stops_at_the_last_step_and_starts_over(open_tour: Any) -> None:
    page = open_tour(clock=True)
    page.click(f"#tour-progress button[data-step='{len(STEPS) - 2}']")
    page.locator("#tour-play").click()
    page.mouse.move(0, 0)
    page.clock.run_for(5_100)
    _at(page, len(STEPS))
    expect(page.locator("#tour-play")).to_have_text("Play")
    page.locator("#tour-play").click()
    _at(page, 1)


def test_a_glossary_term_opens_its_plain_meaning_and_closes(open_tour: Any) -> None:
    page = open_tour()
    term = page.locator("#tour-caption button[data-term]").first
    page.get_by_role("button", name="Technical").click()
    term.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#tour-term")).to_be_visible()
    expect(page.locator("#tour-term-name")).to_have_text("MCP")
    expect(page.locator("#tour-term-analogy")).to_contain_text("In the shop:")
    assert term.get_attribute("aria-expanded") == "true"
    page.keyboard.press("Escape")
    expect(page.locator("#tour-term")).to_be_hidden()
    assert term.get_attribute("aria-expanded") == "false"


def test_focus_is_not_lost_when_the_focused_text_is_replaced(open_tour: Any) -> None:
    page = open_tour()
    page.get_by_role("button", name="Technical").click()
    page.locator("#tour-caption button[data-term]").first.focus()
    page.keyboard.press("ArrowRight")
    _at(page, 2)
    assert _active(page) == "tour-text"
    page.keyboard.press("ArrowRight")
    _at(page, 3)


def test_gate_results_are_written_out_and_drawn(open_tour: Any) -> None:
    page = open_tour()
    number = STEPS.index("trap")
    page.click(f"#tour-progress button[data-step='{number}']")
    states = {
        e["facts"]["gate"]: e["type"].rsplit(".", 1)[1]
        for e in DATA["steps"][number]["events"]
        if e["type"].startswith("forge.gate.") and not e["type"].endswith("started")
    }
    for gate, state in states.items():
        expect(page.locator("#tour-gate-state")).to_contain_text(f"{gate} {state}")
        assert page.locator(f"#ts-{gate.lower()}").get_attribute("data-state") == state
    assert "is-active" in page.locator("#ts-g6").get_attribute("class")


def test_the_last_step_offers_the_commands_and_the_repository(open_tour: Any) -> None:
    page = open_tour(permissions=["clipboard-read", "clipboard-write"])
    expect(page.locator("#tour-start")).to_be_hidden()
    page.locator("#tour-next").focus()
    page.keyboard.press("End")
    expect(page.locator("#tour-start")).to_be_visible()
    link = page.locator("#tour-start a.gh")
    assert link.get_attribute("href") == REPO
    button = page.locator("#tour-start button.copy").first
    button.click()
    expect(button).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()").startswith("git clone")
    button.focus()
    page.keyboard.press("Home")
    _at(page, 1)
    assert _active(page) == "tour-text", "focus is not left on a hidden button"


@pytest.mark.parametrize("width", [360, 375, 768])
def test_no_step_scrolls_the_page_sideways(open_tour: Any, width: int) -> None:
    page = open_tour(width=width)
    for mode in ("simple", "technical"):
        page.locator(f"button[data-mode='{mode}']").click()
        for index, step in enumerate(STEPS):
            page.locator(f"#tour-progress button[data-step='{index}']").click()
            assert page.evaluate("document.documentElement.scrollWidth") <= width, (mode, step)


def test_nothing_moves_when_the_visitor_asks_for_less(open_tour: Any) -> None:
    calm = open_tour(reduced_motion="reduce")
    token = "getComputedStyle(document.querySelector('.ts-token')).transitionDuration"
    assert calm.evaluate(token) == "0s"
    assert (
        calm.evaluate("getComputedStyle(document.querySelector('.ts-part.is-active rect')).animationName")
        == "none"
    )
    calm.click("#tour-next")
    _at(calm, 2)
    moving = open_tour(reduced_motion="no-preference")
    assert moving.evaluate(token) != "0s"


def test_without_javascript_the_twelve_steps_are_a_list(open_tour: Any) -> None:
    page = open_tour(javascript=False)
    items = page.locator("#tour .tour-list > li")
    expect(items).to_have_count(len(STEPS))
    assert items.first.is_visible()
    assert not page.locator("#tour-app").is_visible()
    assert not page.locator("#tour-failed").is_visible()
    assert page.locator("#tour .tour-badge").inner_text() == DATA["badge"]


@pytest.mark.parametrize("broken", ["tour.json", "tour_captions.json", "glossary.json"])
def test_when_a_data_file_cannot_load_the_list_stays(open_tour: Any, broken: str) -> None:
    def cut(page: Any) -> None:
        page.route(f"**/data/{broken}", lambda route: route.fulfill(status=404, body="gone"))

    page = open_tour(setup=cut, live=False)
    expect(page.locator("#tour-failed")).to_be_visible()
    assert page.locator("#tour .tour-list").is_visible()
    assert not page.locator("#tour-app").is_visible()


def test_text_from_a_data_file_is_never_read_as_html(open_tour: Any) -> None:
    hostile = '<img src=x onerror="window.pwned=1">'

    def poison(page: Any) -> None:
        data = json.loads(json.dumps(DATA))
        data["steps"][0]["note"] = hostile
        page.route("**/data/tour.json", lambda route: route.fulfill(json=data))

    page = open_tour(setup=poison)
    expect(page.locator("#tour-note")).to_have_text(hostile)
    assert page.locator("#tour-app img").count() == 0
    assert page.evaluate("window.pwned === undefined")
