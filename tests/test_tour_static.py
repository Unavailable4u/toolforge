"""P20: the tour as built files. It reads without JavaScript, stays still when asked, loads nothing from elsewhere and parses no data as HTML."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from site_helpers import REPO, SITE, build_dist, parse, script

tour = script("build_tour_data")
build_site = script("build_site")
CAPTIONS = json.loads((SITE / "src" / "tour_captions.json").read_text(encoding="utf-8"))
JS = (SITE / "src" / "tour.js").read_text(encoding="utf-8")
CSS = (SITE / "src" / "tour.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_dist(tmp_path_factory.mktemp("tour"))


@pytest.fixture(scope="module")
def section(dist: Path) -> str:
    html = (dist / "index.html").read_text(encoding="utf-8")
    return re.search(r'<section id="tour".*?</section>', html, re.S)[0]


def test_the_written_list_has_all_twelve_steps_in_order(section: str) -> None:
    written = re.search(r'<ol class="[^"]*tour-list">(.*?)</ol>', section, re.S)[1]
    assert re.findall(r'data-tour-step="(\w+)"', written) == list(tour.STEP_IDS)
    text = re.sub(r"<[^>]+>", "", written)
    for step in tour.STEP_IDS:
        assert CAPTIONS[step]["title"] in text
        first_words = re.split(r"[\[`]", CAPTIONS[step]["simple"])[0]
        assert first_words in text, step
    notes = [s["note"] for s in tour.build(SITE.parent / "demo" / "recordings" / "main")["steps"]]
    assert len(re.findall(r'<p class="meta">', written)) == len(notes)
    assert "No event recorded for this step." in text
    assert 'href="#quickstart"' in written


def test_the_written_list_explains_its_terms(section: str) -> None:
    words = re.search(r'<details class="tour-words">(.*?)</details>', section, re.S)[1]
    assert "<summary>" in words
    for step in tour.STEP_IDS:
        for ident in re.findall(r"\[\[([a-z_]+)", CAPTIONS[step]["simple"]):
            glossary = json.loads((SITE / "src" / "glossary.json").read_text(encoding="utf-8"))
            assert f"<dt>{glossary[ident]['term']}</dt>" in words, ident


def test_the_badge_matches_the_source_and_the_caveats_are_visible(section: str) -> None:
    data = json.loads((SITE / "data" / "tour.json").read_text(encoding="utf-8"))
    assert f'<p class="tour-badge tour-badge-{data["source"]}">{data["badge"]}</p>' in section
    visible = section.split('id="tour-app"')[0]
    assert data["badge"] in visible, "the badge is outside the part that needs JavaScript"
    for caveat in data["caveats"]:
        assert caveat.replace("'", "&#x27;") in visible


def test_without_a_tour_file_the_page_is_the_marked_example(tmp_path: Path) -> None:
    site = tmp_path / "site"
    shutil.copytree(SITE, site, ignore=shutil.ignore_patterns("dist"))
    (site / "data" / "tour.json").unlink()
    info = build_site.build(
        tmp_path / "d", site=site, repo_url=REPO, environ={}, git_remote=lambda: None, social=False
    )
    assert any("illustrated example" in warning for warning in info.warnings)
    html = (tmp_path / "d" / "index.html").read_text(encoding="utf-8")
    assert "Illustrated example, not a recording" in html
    assert "Recorded from a real run" not in html
    assert json.loads((tmp_path / "d" / "data" / "tour.json").read_text(encoding="utf-8"))["source"] == (
        "illustrative"
    )


@pytest.mark.parametrize(
    ("name", "change", "message"),
    [
        ("data/tour.json", lambda d: d.update(badge="Recorded"), "tour data is not valid"),
        ("src/tour_captions.json", lambda d: d.pop("trap"), "step trap"),
        (
            "src/tour_captions.json",
            lambda d: d["meet"].update(simple="a [[nonsense]]"),
            "unknown glossary term",
        ),
    ],
)
def test_a_broken_tour_file_stops_the_build(tmp_path: Path, name: str, change, message: str) -> None:
    site = tmp_path / "site"
    shutil.copytree(SITE, site, ignore=shutil.ignore_patterns("dist"))
    path = site / name
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(build_site.SiteError, match=message):
        build_site.build(
            tmp_path / "d", site=site, repo_url=REPO, environ={}, git_remote=lambda: None, social=False
        )


def test_the_built_site_ships_the_tour_files(dist: Path) -> None:
    for name in ("tour.js", "tour.css", "data/tour.json", "data/tour_captions.json", "data/glossary.json"):
        assert (dist / name).is_file(), name
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="tour.css">' in html
    assert '<script src="tour.js" defer></script>' in html
    app = re.search(r'<div id="tour-app"[^>]*>', html)[0]
    for address in re.findall(r'data-(?:tour|captions|glossary)="([^"]+)"', app):
        assert (dist / address).is_file(), address
    assert 'data-interval="5000"' in app


def test_every_id_the_script_asks_for_is_on_the_page(dist: Path) -> None:
    ids = set(parse(dist / "index.html").ids)
    asked = set(re.findall(r'\bel\("([\w-]+)"\)', JS))
    assert asked and asked <= ids, sorted(asked - ids)


def test_the_picture_has_every_part_the_script_highlights(dist: Path) -> None:
    ids = set(parse(dist / "index.html").ids)
    block = re.search(r"var PARTS = \{(.*?)\n  \};", JS, re.S)[1]
    steps = re.findall(r"^\s*(\w+): \[", block, re.M)
    assert steps == list(tour.STEP_IDS)
    parts = set(re.findall(r'"(\w+)"', block)) | {g.lower() for gs in tour.STEP_GATES.values() for g in gs}
    assert {f"ts-{part}" for part in parts} <= ids
    for step in re.findall(r'data-step="([a-z_]+)"', CSS):
        assert step in tour.STEP_IDS, step
    assert f"var STEP_COUNT = {len(tour.STEP_IDS)};" in JS


def test_the_progress_list_and_the_controls_have_names(section: str) -> None:
    buttons = re.findall(r'<button type="button" data-step="(\d+)" aria-label="Step (\d+): [^"]+"', section)
    assert [(int(a), int(b)) for a, b in buttons] == [(n, n + 1) for n in range(len(tour.STEP_IDS))]
    assert section.count('aria-current="step"') == 1
    assert 'aria-live="polite"' in section
    assert re.search(r'id="tour-play" aria-pressed="false"', section), "play is off until asked"
    assert 'data-mode="simple" aria-pressed="true"' in section
    assert section.count("<h3") >= len(tour.STEP_IDS)


def test_the_interactive_part_shows_only_once_the_script_has_its_data() -> None:
    assert re.search(r"\.tour-app\s*\{[^}]*display:\s*none", CSS)
    assert re.search(r"\.tour-live \.tour-app\s*\{[^}]*display:\s*block", CSS)
    assert re.search(r"\.tour-live \.tour-static\s*\{[^}]*display:\s*none", CSS)
    assert 'classList.add("tour-live")' in JS
    assert JS.index("show();\n    section.classList.add") > 0, "shown only after the first step rendered"


def test_reduced_motion_stops_the_stage() -> None:
    block = CSS[CSS.index("@media (prefers-reduced-motion: reduce)") :]
    assert ".ts-token" in block and "transition: none" in block
    assert ".is-active rect" in block and "animation: none" in block
    for rule in re.findall(r"^([^@{}\n][^{}]*)\{[^}]*\banimation:\s*(?!none)", CSS, re.M):
        assert rule.strip() in block, f"{rule.strip()} moves and is not stopped"


def test_the_stylesheet_uses_only_the_theme_tokens_and_loads_nothing() -> None:
    rules = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b(?![-\w])(?=[^{}]*;)", rules), "colour comes from styles.css"
    assert not re.search(r"\b(?:rgb|rgba|hsl)\(", rules)
    for banned in ("url(", "@import", "@font-face"):
        assert banned not in rules
    tokens = set(re.findall(r"--([\w-]+):", (SITE / "src" / "styles.css").read_text(encoding="utf-8")))
    assert set(re.findall(r"var\(--([\w-]+)\)", rules)) <= tokens


def test_the_script_parses_no_data_as_html_and_reaches_only_its_own_files() -> None:
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "DOMParser",
        "eval(",
        "new Function",
        "XMLHttpRequest",
        "WebSocket",
        "sendBeacon",
        "EventSource",
        "import(",
        "document.cookie",
        "http://",
        "https://",
    ):
        assert banned not in JS, banned
    assert JS.count("fetch(") == 1
    assert re.findall(r"load\(app\.getAttribute\(\"data-(\w+)\"\)\)", JS) == ["tour", "captions", "glossary"]


def test_the_only_thing_stored_is_the_reading_mode_and_storage_may_fail() -> None:
    assert set(re.findall(r"localStorage\.(?:get|set|remove)Item\(\s*(\w+)", JS)) == {"MODE_KEY"}
    assert 'var MODE_KEY = "toolforge-tour-mode"' in JS
    for match in re.finditer(r"window\.localStorage", JS):
        before = JS[: match.start()]
        assert before.rfind("try {") > before.rfind("function "), "storage is read inside try"


def test_the_hero_still_leads_to_the_tour_and_the_feature_is_claimed(dist: Path) -> None:
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert 'href="#tour">Take the 2-minute tour</a>' in html
    config = json.loads((SITE / "site.json").read_text(encoding="utf-8"))
    row = next(r for r in config["features"] if r["name"] == "Interactive guided tour")
    assert (row["chip"], row["patches"], row["doc"]) == ("built", ["P20"], "docs/SITE.md")
