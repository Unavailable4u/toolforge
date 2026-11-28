"""P19: the page's structure, accessibility and CSP-compatibility, checked on the built HTML and the stylesheet."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from site_helpers import REPO, SITE, build_dist, parse

SECTIONS = [
    "top",
    "problem",
    "idea",
    "tour",
    "architecture",
    "gates",
    "capabilities",
    "features",
    "security",
    "proof",
    "tools",
    "quickstart",
    "built",
    "limits",
    "faq",
]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_dist(tmp_path_factory.mktemp("html"))


@pytest.fixture(scope="module")
def html_text(built: Path) -> str:
    return (built / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page(built: Path):
    return parse(built / "index.html")


def test_the_html_is_well_formed(page) -> None:
    assert page.errors == []


def test_one_h1_and_no_skipped_heading_levels(page) -> None:
    levels = [level for level, _ in page.headings]
    assert levels.count(1) == 1
    assert levels[0] == 1
    for before, after in zip(levels, levels[1:], strict=False):
        assert after <= before + 1, f"a heading jumps from h{before} to h{after}"
    assert all(text for _, text in page.headings), "an empty heading"


def test_ids_are_unique_and_every_in_page_link_lands(page) -> None:
    assert len(page.ids) == len(set(page.ids)), sorted({i for i in page.ids if page.ids.count(i) > 1})
    for link in page.links:
        href = link["href"]
        if href.startswith("#") and len(href) > 1:
            assert href[1:] in page.ids, f"{href} points nowhere"


def test_the_page_map_is_all_there(page) -> None:
    for section in SECTIONS:
        assert section in page.ids, section
    assert "main" in page.ids
    nav = [a["href"] for a in page.links if a["href"].startswith("#")]
    for target in (
        "#idea",
        "#tour",
        "#architecture",
        "#gates",
        "#features",
        "#security",
        "#proof",
        "#quickstart",
    ):
        assert target in nav


def test_every_section_names_itself_and_opens_with_a_summary(html_text: str) -> None:
    blocks = re.findall(r'<section id="(\w+)"([^>]*)>(.*?)</section>', html_text, re.S)
    assert [b[0] for b in blocks] == SECTIONS
    for ident, attrs, body in blocks:
        label = re.search(r'aria-labelledby="([\w-]+)"', attrs)
        assert label, f"section {ident} has no aria-labelledby"
        assert f'id="{label[1]}"' in body, (
            f"section {ident} labels itself with a heading that is not inside it"
        )
        if ident != "top":
            assert 'class="summary"' in body, f"section {ident} has no one-sentence summary"


def test_the_skip_link_comes_first_and_works(page) -> None:
    tag, attrs = page.focusable[0]
    assert tag == "a"
    assert attrs["href"] == "#main"
    assert "skip" in attrs["class"]


def test_the_head_is_complete(html_text: str) -> None:
    assert '<html lang="en">' in html_text
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html_text
    title = re.search(r"<title>(.+?)</title>", html_text)
    assert title and 20 < len(title[1]) < 90
    description = re.search(r'<meta name="description" content="([^"]+)"', html_text)
    assert description and 60 < len(description[1]) < 320
    assert 'rel="icon" type="image/svg+xml" href="assets/favicon.svg"' in html_text
    assert 'property="og:title"' in html_text
    assert 'name="twitter:card"' in html_text


def test_exactly_one_github_button_in_the_header(page, built: Path) -> None:
    header = re.search(r"<header.*?</header>", (built / "index.html").read_text(encoding="utf-8"), re.S)
    assert header
    buttons = re.findall(r"<a [^>]*>", header[0])
    github = [tag for tag in buttons if f'href="{REPO}"' in tag]
    assert len(github) == 1
    assert 'target="_blank"' in github[0]
    assert 'rel="noopener noreferrer"' in github[0]
    assert 'aria-label="ToolForge on GitHub"' in github[0]


def test_links_that_open_a_new_tab_are_safe(page) -> None:
    for link in page.links:
        if link.get("target") == "_blank":
            assert "noopener" in link.get("rel", ""), link["href"]


def test_nothing_needs_unsafe_inline_under_the_csp(html_text: str) -> None:
    assert "default-src 'self'" in html_text
    assert not re.search(r"<style[\s>]", html_text), "an inline <style> would be blocked by the CSP"
    assert not re.search(r"\sstyle=", html_text), "an inline style attribute would be blocked by the CSP"
    assert not re.search(r"<script(?![^>]*\ssrc=)", html_text), "an inline script would be blocked by the CSP"
    assert not re.search(r"\son[a-z]+=", html_text), "an inline event handler would be blocked by the CSP"
    assert not re.search(r"javascript:", html_text)


def test_images_text_alternatives_and_decoration(page, html_text: str) -> None:
    for tag, attrs in page.elements:
        if tag == "img":
            assert "alt" in attrs
    assert 'role="img" aria-labelledby="arch-title arch-desc"' in html_text
    assert '<title id="arch-title">' in html_text
    assert '<desc id="arch-desc">' in html_text
    for tag, attrs in page.elements:
        if tag == "svg" and attrs.get("role") != "img":
            assert attrs.get("aria-hidden") == "true", (
                "a decorative svg must be hidden from assistive technology"
            )
            assert attrs.get("focusable") == "false"


def test_controls_have_names_and_tables_have_captions(page, html_text: str) -> None:
    for opening, inner in re.findall(r"(<button\b[^>]*>)(.*?)</button>", html_text, re.S):
        assert re.sub(r"<[^>]+>", "", inner).strip() or "aria-label=" in opening, opening
    for tag in re.findall(r"<button\b[^>]*>", html_text):
        assert 'type="button"' in tag
    tables = html_text.count("<table")
    assert (
        tables >= 4
    )  # tools, settings, capabilities, attacks; the benchmark and context tables appear once measured
    assert html_text.count("<caption") == tables
    assert html_text.count('<th scope="col">') >= tables * 2
    assert 'aria-label="Switch between light and dark colours"' in html_text
    assert 'aria-expanded="false" aria-controls="site-nav"' in html_text


def test_scrollable_regions_can_be_reached_by_keyboard(html_text: str) -> None:
    for wrap in re.findall(r'<div class="table-wrap"[^>]*>', html_text):
        assert 'tabindex="0"' in wrap and 'role="region"' in wrap and "aria-label=" in wrap
    assert '<figure class="diagram" tabindex="0">' in html_text


def test_every_status_chip_uses_one_of_the_three_words(html_text: str) -> None:
    chips = re.findall(r'<span class="chip chip-(\w+)">([^<]*)</span>', html_text)
    assert chips
    for kind, word in chips:
        assert kind in {"built", "experimental", "planned"}
        if kind != "experimental" or "optional" not in word:
            assert word == kind


def test_plain_language_rules_hold(html_text: str) -> None:
    text = re.sub(r"<[^>]+>", " ", html_text).lower()
    for banned in (
        "revolutionary",
        "seamless",
        "cutting-edge",
        "game-changing",
        "supercharge",
        "next-generation",
        "world-class",
    ):
        assert banned not in text, banned
    hero = html_text[html_text.index('id="top"') : html_text.index('id="problem"')]
    assert "Take the 2-minute tour" in hero
    assert 'href="#tour"' in hero and 'href="#quickstart"' in hero
    first_sentence = re.sub(r"<[^>]+>", "", re.search(r'<p class="lead">(.*?)</p>', hero, re.S)[1])
    assert "MCP" not in first_sentence, "the first thing a reader sees must not be an acronym"


def test_the_page_works_without_javascript(html_text: str) -> None:
    css = (SITE / "src" / "styles.css").read_text(encoding="utf-8")
    # Anything that needs the script is hidden until the script adds the `js` class.
    for selector in (".theme-toggle", ".menu-toggle", ".copy"):
        assert re.search(rf"{re.escape(selector)}[^{{]*{{[^}}]*display:\s*none", css), selector
    assert "details" in html_text and "<summary>" in html_text
    assert 'class="site-nav"' in html_text


# --- Contrast of the colour tokens, both themes ---------------------------------------------------------------------------------


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def _themes() -> dict[str, dict[str, str]]:
    css = (SITE / "src" / "styles.css").read_text(encoding="utf-8")
    light = _tokens(css[css.index(":root {") : css.index(':root[data-theme="dark"]')])
    dark_block = css[css.index(':root[data-theme="dark"]') : css.index("@media (prefers-color-scheme: dark)")]
    media_block = css[css.index("@media (prefers-color-scheme: dark)") : css.index("*,\n*::before")]
    return {
        "light": light,
        "dark": {**light, **_tokens(dark_block)},
        "dark-by-system": {**light, **_tokens(media_block)},
    }


PAIRS = [
    ("fg", "bg"),
    ("fg", "band"),
    ("fg", "surface"),
    ("muted", "bg"),
    ("muted", "band"),
    ("muted", "surface"),
    ("link", "bg"),
    ("link", "band"),
    ("link", "surface"),
    ("pass", "surface"),
    ("pass", "pass-bg"),
    ("warn", "warn-bg"),
    ("fail", "surface"),
    ("accent-ink", "accent"),
    ("bg", "fg"),
]


@pytest.mark.parametrize("theme", ["light", "dark", "dark-by-system"])
def test_text_colours_meet_aa_contrast(theme: str) -> None:
    tokens = _themes()[theme]
    for foreground, background in PAIRS:
        ratio = _ratio(tokens[foreground], tokens[background])
        assert ratio >= 4.5, f"{theme}: {foreground} on {background} is {ratio:.2f}:1"


def test_the_two_dark_blocks_agree() -> None:
    themes = _themes()
    assert themes["dark"] == themes["dark-by-system"], (
        "the explicit dark theme and the system dark theme must be the same colours"
    )
