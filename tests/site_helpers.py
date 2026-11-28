"""Shared pieces for the website tests (P19): load the build scripts, make realistic result files, build into a temp dir, parse HTML.

The result files are made by the repository's own writers (`write_json` of the Attack Lab, `bench.report.summarise`,
`bench.context.measure`), not typed by hand, so a change in their shape breaks these tests instead of the live site.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
REPO = "https://github.com/example-owner/toolforge"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def script(name: str) -> ModuleType:
    path = str(ROOT / "scripts")
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module(name)


def sample_runs(root: Path, *, attacks: bool = True, bench: bool = True, context: bool = True) -> Path:
    """A `runs/` directory shaped like the real one. Returns its path."""
    from toolforge.attacks.catalog import ATTACKS
    from toolforge.attacks.runner import AttackResult, write_json

    runs = root / "runs"
    if attacks:
        verdicts = ["inconclusive", "skipped"] + ["contained"] * (len(ATTACKS) - 2)
        results = [AttackResult(a.id, a.title, v, "evidence") for a, v in zip(ATTACKS, verdicts, strict=True)]
        write_json(results, runs / "attacks", docker="27.0.1")
    if bench or context:
        sys.path.insert(0, str(ROOT))
        try:
            from bench import context as bench_context
            from bench import report
        finally:
            sys.path.remove(str(ROOT))
        records = []
        for config, wins in (("baseline", 1), ("forge_cold", 3), ("forge_warm", 4)):
            for i in range(5):
                ok = i < wins
                records.append(
                    {
                        "config": config,
                        "task": "log_stats",
                        "tier": "1",
                        "run": i,
                        "outcome": "success" if ok else "failure",
                        "success": ok,
                    }
                )
        measured = bench_context.measure((10, 100), seed=1) if context else None
        summary = report.summarise(
            records if bench else [], context=measured, now=dt.datetime(2026, 11, 20, 8, 0, tzinfo=dt.UTC)
        )
        folder = runs / "bench" / "20261120T080000Z"
        folder.mkdir(parents=True)
        (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return runs


def build_data(runs: Path, out: Path, *, generated_at: str = "2026-11-21") -> dict[str, Any]:
    script("build_site_data").main(["--runs", str(runs), "--out", str(out), "--generated-at", generated_at])
    return json.loads(out.read_text(encoding="utf-8"))


def build_dist(
    tmp_path: Path, *, results: dict[str, Any] | None = None, name: str = "dist", release: bool = False
) -> Path:
    """Build the real site into `tmp_path/name`. Without `results` the committed snapshot is used."""
    results_path = None
    if results is not None:
        results_path = tmp_path / f"{name}-results.json"
        results_path.write_text(json.dumps(results), encoding="utf-8")
    script("build_site").build(
        tmp_path / name,
        repo_url=REPO,
        release=release,
        results_path=results_path,
        environ={},
        git_remote=lambda: None,
        social=False,
    )
    return tmp_path / name


class Page(HTMLParser):
    """Collects what the structural tests ask about: tags in order, ids, links, headings, text per section."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.links: list[dict[str, str]] = []
        self.headings: list[tuple[int, str]] = []
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.focusable: list[tuple[str, dict[str, str]]] = []
        self.section_text: dict[str, list[str]] = {}
        self.section_html_fig: dict[str, list[str]] = {}
        self._heading: list[str] | None = None
        self._fig = 0
        self._fig_text: list[str] = []
        self._section: str | None = None
        self._section_depth = 0
        self.feed(text)
        self.close()
        if self.stack:
            self.errors.append(f"unclosed: {self.stack}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        self.elements.append((tag, a))
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "a" and "href" in a:
            self.links.append(a)
        if tag in {"a", "button", "summary"} or a.get("tabindex") == "0":
            if "hidden" not in a:
                self.focusable.append((tag, a))
        if re.fullmatch(r"h[1-6]", tag):
            self._heading = []
        if tag == "section" and "id" in a and self._section is None:
            self._section, self._section_depth = a["id"], 0
            self.section_text[self._section] = []
            self.section_html_fig[self._section] = []
        if self._section and tag == "section":
            self._section_depth += 1
        if self._section and tag == "span" and "fig" in a.get("class", "").split():
            self._fig += 1
            self._fig_text = []
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1:] or 'nothing'}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()
        if re.fullmatch(r"h[1-6]", tag) and self._heading is not None:
            self.headings.append((int(tag[1]), "".join(self._heading).strip()))
            self._heading = None
        if self._section and tag == "span" and self._fig:
            self._fig -= 1
            if self._fig == 0:
                self.section_html_fig[self._section].append("".join(self._fig_text).strip())
        if self._section and tag == "section":
            self._section_depth -= 1
            if self._section_depth <= 0:
                self._section = None

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)
        if self._section:
            self.section_text[self._section].append(data)
            if self._fig:
                self._fig_text.append(data)


def parse(path: Path) -> Page:
    return Page(path.read_text(encoding="utf-8"))


def numbers_in(value: Any) -> set[str]:
    """Every way a figure from results.json may legitimately be printed: as stored, or as a whole percent of a fraction."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key).isdigit():
                    found.add(str(key))
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            found.add(str(node))
            if isinstance(node, float) and 0 <= node <= 1:
                found.add(f"{round(node * 100)}%")

    walk(value)
    return found
