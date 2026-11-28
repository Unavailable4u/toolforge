"""P18's documents: they exist, they say what the code says, and the ones the website parses keep their format."""

from __future__ import annotations

import re
import subprocess
import sys
import xml.dom.minidom
from pathlib import Path

import pytest

from demo import acts
from toolforge.verify.pipeline import GATE_INFO, gates_markdown

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def links(markdown: str) -> list[str]:
    found = re.findall(r"\]\(([^)\s]+)\)", markdown) + re.findall(
        r"`((?:docs|demo|scripts|deploy|examples)/[\w./-]+\.(?:md|py|json|svg|yml))`", markdown
    )
    return [
        link.split("#")[0] for link in found if not re.match(r"^[a-z]+://", link) and not link.startswith("#")
    ]


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/PROJECT_SUMMARY.md",
        "docs/DEMO_SCRIPT.md",
        "docs/KNOWN_LIMITATIONS.md",
        "docs/clients.md",
    ],
)
def test_every_file_a_document_points_to_exists(name: str) -> None:
    base = (ROOT / name).parent
    for link in links(read(name)):
        if "<" in link or "*" in link or link.endswith("/"):
            continue
        assert (base / link).exists() or (ROOT / link).exists(), (
            f"{name} points to {link}, which does not exist"
        )


def test_the_readme_replaced_the_starter_text() -> None:
    text = read("README.md")
    assert (
        "Starter:" not in text
        and "replaced by the" not in text
        and "Start here (first ten minutes)" not in text
    )
    for needle in (
        "toolforge promote",
        "make demo-up",
        "run_demo.py --replay",
        "KNOWN_LIMITATIONS.md",
        "ARCHITECTURE.md",
        "<!-- tools:begin -->",
    ):
        assert needle in text, needle


def test_the_readme_commits_to_no_measured_number() -> None:
    text = read("README.md")
    assert not re.search(r"\b\d{1,3}(\.\d+)?\s?%", text), "results belong to result files, not to prose"
    assert "No results are committed" in text


def test_the_project_summary_is_short_enough_to_paste_into_a_form() -> None:
    text = read("docs/PROJECT_SUMMARY.md")
    assert len(re.findall(r"\S+", text)) < 400
    for heading in ("The problem", "The solution", "How it works", "Integrations", "Links"):
        assert f"## {heading}" in text
    assert len(re.findall(r"^\* \*\*", text.split("## How it works")[1].split("##")[0], re.M)) == 5


BULLET = re.compile(r"^\* \*\*[^*\n]+\.\*\* \S[^\n]*$")


def test_known_limitations_keeps_the_format_the_website_parses() -> None:
    bullets = [line for line in read("docs/KNOWN_LIMITATIONS.md").splitlines() if line.startswith("*")]
    assert len(bullets) >= 7
    for line in bullets:
        assert BULLET.match(line), line
        body = line.split("** ", 1)[1]
        sentences = re.findall(r"[^.!?]+[.!?](?:\s|$)", body)
        assert 1 <= len(sentences) <= 3, line
    wanted = (
        "egress allowlist",
        "challenger",
        "static",
        "python only",
        "pixel",
        "cold start",
        "one operator",
    )
    titles = " ".join(re.findall(r"^\* \*\*(.+?)\.\*\*", read("docs/KNOWN_LIMITATIONS.md"), re.M)).lower()
    for needle in wanted:
        assert needle in titles, needle


def test_the_limitations_do_not_claim_numbers() -> None:
    assert not re.search(r"\b\d+(\.\d+)?\s?%", read("docs/KNOWN_LIMITATIONS.md"))


def test_the_cover_is_a_self_contained_svg() -> None:
    text = read("docs/assets/cover.svg")
    dom = xml.dom.minidom.parseString(text)
    root = dom.documentElement
    assert (root.getAttribute("width"), root.getAttribute("height")) == ("1200", "630")
    assert (
        not re.search(r"(href|src)=\"(https?:|data:image)", text)
        and "<image" not in text
        and "<script" not in text
    )
    assert "ToolForge" in text


def test_the_render_script_names_every_converter_and_fails_loudly_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import render_cover
    finally:
        sys.path.pop(0)
    names = [name for name, _ in render_cover.commands(render_cover.SVG, tmp_path / "x.png")]
    assert names == ["rsvg-convert", "inkscape", "magick", "convert"]
    monkeypatch.setattr(render_cover.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "cairosvg", None)
    assert render_cover.render(render_cover.SVG, tmp_path / "x.png") is None
    assert not (tmp_path / "x.png").exists(), "no blank file may be left behind"
    assert render_cover.main(["--out", str(tmp_path / "y.png")]) == 2


def test_the_slide_outline_is_what_the_script_writes_without_results() -> None:
    done = subprocess.run(
        [sys.executable, "scripts/build_slides.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_slide_outline_has_ten_slides_and_says_not_measured_yet() -> None:
    text = read("docs/slides.md")
    assert text.startswith("---\nmarp: true")
    body = text.split("---\n", 2)[2]
    assert len(body.split("\n---\n")) == 10
    assert text.count("Not measured yet") == 2
    assert not re.search(r"\b\d+\s?%", text), "a slide number must come from a result file"


def test_slides_fill_in_from_result_files(tmp_path: Path) -> None:
    import json

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import build_slides
    finally:
        sys.path.pop(0)
    (tmp_path / "bench" / "run1").mkdir(parents=True)
    summary = {
        "model": "m",
        "generated_at": "2026-01-01T00:00:00Z",
        "configs": {
            "baseline": {
                "overall": {
                    "runs": 10,
                    "successes": 3,
                    "success_rate": 0.3,
                    "wilson_95": [0.1, 0.6],
                    "median_tool_calls": None,
                }
            }
        },
    }
    (tmp_path / "bench" / "run1" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    text = build_slides.render(tmp_path)
    assert "| baseline | 10 | 3 | 30% | 10% to 60% | not measured |" in text
    assert text.count("Not measured yet") == 1, "only the attack slide is still empty"


def test_the_architecture_gate_table_is_the_one_in_the_code() -> None:
    text = read("docs/ARCHITECTURE.md")
    inside = text.split("<!-- gates:begin -->\n")[1].split("\n<!-- gates:end -->")[0]
    assert inside == gates_markdown()
    for gate in GATE_INFO:
        assert gate.id in inside


def test_the_architecture_names_modules_that_exist() -> None:
    text = read("docs/ARCHITECTURE.md")
    for path in re.findall(r"`(toolforge(?:\.[a-z_]+)+)`", text):
        module = path.replace(".", "/")
        assert (ROOT / "src" / f"{module}.py").is_file() or (
            ROOT / "src" / module / "__init__.py"
        ).is_file(), path
    for relative in ("registry/lifecycle.py", "server/tools.py", "server/fences.py", "sandbox/_ml_path.py"):
        assert (ROOT / "src" / "toolforge" / relative).is_file(), relative


def test_the_demo_script_lists_the_acts_from_the_code_and_the_commands() -> None:
    text = read("docs/DEMO_SCRIPT.md")
    inside = text.split("<!-- acts:begin -->\n")[1].split("\n<!-- acts:end -->")[0]
    for act in acts.ACTS:
        assert f"| {act.number} | {act.title} (`{act.id}`) | `{act.task}`" in inside and act.shows in inside
    for command in (
        "python demo/run_demo.py --replay demo/transcripts/",
        "--record demo/recordings/main/",
        "toolforge images build",
        "make demo-up",
        "make cover",
    ):
        assert command in text, command
    assert "scripted" in text and "stand-in" in text.lower()
    minutes = re.findall(r"^\| (\d):(\d\d) \|", text, re.M)
    seconds = [int(m) * 60 + int(s) for m, s in minutes]
    assert seconds == sorted(seconds) and 180 <= seconds[-1] <= 240, "a 3 to 4 minute shot list"


def test_the_clients_guide_has_the_compose_setup_and_the_http_configuration() -> None:
    text = read("docs/clients.md")
    for needle in (
        "2b. Start it with Docker Compose",
        "127.0.0.1:8765/mcp",
        "Authorization: Bearer",
        "TOOLFORGE_EGRESS_PROXY_IMAGE",
        "Not yet proven",
    ):
        assert needle in text, needle


def test_the_deviations_and_progress_record_p18() -> None:
    assert "(P18)" in read("docs/guide/DEVIATIONS.md")
    progress = read("PROGRESS.md")
    assert "| [x] | P18 |" in progress and "P18 not run, on purpose" in progress
