"""Build the static website: `site/src` to `site/dist` (P19).

    python scripts/build_site.py                 # a local preview build
    python scripts/build_site.py --release       # what the Pages workflow runs: refuses a placeholder repository address
    python scripts/build_site.py --repo-url https://github.com/OWNER/NAME --dist /tmp/site

No Node and no bundler. The page skeleton (`site/src/index.html`) holds `{{placeholders}}`; each is filled from the one place that already
knows the answer, so the site never hand-copies a list that the code owns:

    {{tools_table}}        `META_TOOLS`   (toolforge/server/tools.py)
    {{gates_cards}}        `GATE_INFO`    (toolforge/verify/pipeline.py)
    {{settings_table}}     the `Settings` fields, described by the comments in `.env.example`
    {{attacks_table}}      `ATTACKS`      (toolforge/attacks/catalog.py) joined with the newest Attack Lab result in results.json
    {{bench_table}}, {{bench_meta}}, {{context_table}}   `site/data/results.json`; absent results draw the hatched "Not measured yet"
    {{tour_intro}}, {{tour_progress}}, {{tour_first_*}}, {{tour_quickstart}}, {{tour_steps}}, {{tour_glossary}}   the guided tour (P20):
                            `site/data/tour.json` (made by `build_tour_data.py`), `src/tour_captions.json` and `src/glossary.json`
    {{limitations}}        the bullets of `docs/KNOWN_LIMITATIONS.md`
    {{quickstart}}, {{capabilities_table}}, {{features_list}}   `site/site.json` (checked against README, PROGRESS.md and the docs by tests)
    {{repo_url}} {{title}} {{tagline}} {{description}} {{version}} {{generated_on}} {{site_url}} {{social_meta}}

`{{partial:NAME}}` includes `site/src/partials/NAME.html`; `{{svg:NAME}}` and `{{icon:NAME}}` inline `site/src/assets/NAME.svg` and
`site/src/assets/gates/NAME.svg` (inline SVG can follow the colour theme; an `<img>` cannot).

**Every number that comes from a measurement is wrapped in `<span class="fig">`** and comes from `results.json`, so a test can check them
all. A missing result is never a zero. **The repository address is never typed into a script**: `--repo-url`, then `site/site.json`, then
`GITHUB_REPOSITORY`, then `git remote get-url origin`; the build says which one it used. A placeholder address (it contains `OWNER`) is
fine for a preview and is refused by `--release`, because a dead GitHub button must not ship.

The output is deterministic: sorted iteration, LF line endings, no clock (the only date is `generated_at` from `results.json`). The build
makes no network request. It needs the `toolforge` package (for the lists above) and nothing else outside the standard library.

Proven by `tests/test_site_build.py`, `tests/test_site_html.py`, `tests/test_site_offline.py` and `tests/test_site_data.py`.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
PLACEHOLDER_OWNER = "OWNER"
PLACEHOLDER_REPO_URL = f"https://github.com/{PLACEHOLDER_OWNER}/toolforge"
CHIPS = ("built", "experimental", "planned")
CONFIG_LABELS = {
    "baseline": "Baseline: one tool, run_python",
    "forge_cold": "ToolForge, empty registry",
    "forge_warm": "ToolForge, after solving a sibling task",
}
TOKEN = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)(?::([A-Za-z0-9_.-]+))?\s*\}\}")
SHELL_STYLE_COMMENT = re.compile(r"<!--.*?-->", re.S)


class SiteError(Exception):
    """A build problem the person can fix; `main` prints it and exits 1."""


# --- The repository address ---------------------------------------------------------------------------------------------------

_GITHUB_FORMS = (
    re.compile(r"^git@(?:www\.)?github\.com:(?P<o>[\w.-]+)/(?P<n>[\w.-]+?)(?:\.git)?/?$"),
    re.compile(
        r"^(?:ssh|git\+ssh)://git@(?:www\.)?github\.com(?::\d+)?/(?P<o>[\w.-]+)/(?P<n>[\w.-]+?)(?:\.git)?/?$"
    ),
    re.compile(
        r"^(?:https?|git)://(?:[^@/\s]+@)?(?:www\.)?github\.com/(?P<o>[\w.-]+)/(?P<n>[\w.-]+?)(?:\.git)?/?$"
    ),
)
_OWNER_NAME = re.compile(r"^(?P<o>[\w.-]+)/(?P<n>[\w.-]+)$")


def normalise_repo_url(value: str) -> str:
    """`https://github.com/<owner>/<name>` from an ssh or https remote (credentials, `.git` and a trailing slash dropped).

    Anything that is not a GitHub address is a `SiteError`: the button must go somewhere real, and a token typed into a remote must
    never reach a published page.
    """
    text = value.strip()
    for form in _GITHUB_FORMS:
        if match := form.match(text):
            return f"https://github.com/{match['o']}/{match['n']}"
    raise SiteError(
        f"{value!r} is not a GitHub repository address. Use https://github.com/<owner>/<name> "
        "(an ssh remote is converted for you)."
    )


def _git_origin(root: Path = ROOT) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def resolve_repo_url(
    cli: str | None,
    site_value: str | None,
    environ: Mapping[str, str],
    git_remote: Callable[[], str | None] = _git_origin,
) -> tuple[str, str]:
    """`(url, where it came from)`: `--repo-url`, then `site.json`, then `GITHUB_REPOSITORY`, then the git remote, then a placeholder.

    The first source that is present is the one used; if it is not a GitHub address that is an error, never a fall-through to the next.
    """
    if cli and cli.strip():
        return normalise_repo_url(cli), "--repo-url"
    if site_value and site_value.strip():
        return normalise_repo_url(site_value), "site/site.json"
    if (slug := environ.get("GITHUB_REPOSITORY", "").strip()) and (match := _OWNER_NAME.match(slug)):
        return f"https://github.com/{match['o']}/{match['n']}", "GITHUB_REPOSITORY"
    if environ.get("GITHUB_REPOSITORY", "").strip():
        raise SiteError(
            f"GITHUB_REPOSITORY is {environ['GITHUB_REPOSITORY']!r}; it should look like owner/name."
        )
    if remote := git_remote():
        return normalise_repo_url(remote), "git remote origin"
    return PLACEHOLDER_REPO_URL, "nothing (placeholder)"


def is_placeholder(url: str) -> bool:
    return f"/{PLACEHOLDER_OWNER}/" in url or url.endswith(f"/{PLACEHOLDER_OWNER}")


def default_site_url(repo_url: str) -> str:
    owner, name = repo_url.removeprefix("https://github.com/").split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


# --- Small HTML helpers -------------------------------------------------------------------------------------------------------


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def inline_code(text: str) -> str:
    """Escape `text`; a `backtick` span becomes <code>. Nothing else is interpreted."""
    out = []
    for part in re.split(r"(`[^`]*`)", text):
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1], quote=False)}</code>")
        else:
            out.append(html.escape(part, quote=False))
    return "".join(out)


def chip(status: str) -> str:
    if status not in CHIPS:
        raise SiteError(f"status {status!r} is not one of {', '.join(CHIPS)}")
    return f'<span class="chip chip-{status}">{status}</span>'


def fig(value: Any) -> str:
    """A measured number. Everything wrapped here must be derivable from results.json (a test checks it)."""
    return f'<span class="fig">{esc(value)}</span>'


NOT_MEASURED = '<span class="nm">Not measured yet</span>'


def table(caption: str, header: list[str], rows: list[list[str]], *, css: str = "") -> str:
    """A table that scrolls on its own, never the page. `rows` hold HTML cells."""
    head = "".join(f'<th scope="col">{h}</th>' for h in header)
    body = "".join("<tr>" + "".join(cells) + "</tr>" for cells in rows)
    return (
        f'<div class="table-wrap" tabindex="0" role="region" aria-label="{esc(caption)}">'
        f'<table class="{css}"><caption class="sr-only">{esc(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def td(content: str, css: str = "") -> str:
    return f'<td class="{css}">{content}</td>' if css else f"<td>{content}</td>"


def th_row(content: str) -> str:
    return f'<th scope="row">{content}</th>'


# --- Generated sections -------------------------------------------------------------------------------------------------------


def _toolforge() -> Any:
    """Import the package; from a checkout that is not installed, use `src/`."""
    try:
        import toolforge  # noqa: PLC0415
    except ImportError:
        sys.path.insert(0, str(ROOT / "src"))
        import toolforge  # noqa: PLC0415
    return toolforge


def tools_table() -> str:
    _toolforge()
    from toolforge.server.tools import META_TOOLS, OPTIONAL_TOOLS  # noqa: PLC0415

    rows = []
    for tool in META_TOOLS:
        optional = (
            f' <span class="chip chip-experimental">optional: <code>TOOLFORGE_{OPTIONAL_TOOLS[tool.name].upper()}</code></span>'
            if tool.name in OPTIONAL_TOOLS
            else ""
        )
        rows.append(
            [
                th_row(f"<code>{esc(tool.name)}</code>{optional}"),
                td(inline_code(tool.summary)),
                td(inline_code(tool.inputs)),
                td(inline_code(tool.returns)),
                td(esc(tool.since)),
            ]
        )
    return table(
        "The MCP tools ToolForge offers an agent",
        ["Tool", "What it does", "Input", "Returns", "Since"],
        rows,
        css="tools",
    )


def gates_cards(root: Path = SITE / "src") -> str:
    _toolforge()
    from toolforge.verify.pipeline import GATE_INFO  # noqa: PLC0415

    cards = []
    for gate in GATE_INFO:
        icon = inline_svg(root / "assets" / "gates" / f"{gate.id.lower()}.svg")
        cards.append(
            f'<article class="card gate" id="gate-{gate.id.lower()}">'
            f'<div class="gate-top"><span class="gate-icon" aria-hidden="true">{icon}</span>'
            f'<span class="gate-id">{esc(gate.id)}</span>{chip(gate.status)}</div>'
            f"<h3>{esc(gate.name)}</h3>"
            f"<p>{inline_code(gate.plain_summary)}</p>"
            f'<p class="gate-line"><strong>What it catches.</strong> {inline_code(gate.catches)}</p>'
            f'<p class="gate-line"><strong>For example.</strong> {inline_code(gate.example)}</p>'
            f"</article>"
        )
    return '<div class="grid gates">' + "".join(cards) + "</div>"


def _env_example(path: Path) -> dict[str, tuple[str, str]]:
    """`{ENV_NAME: (default text, comment)}` from the commented example lines in `.env.example`."""
    found: dict[str, tuple[str, str]] = {}
    comment: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            comment = []
            continue
        setting = re.match(r"^#?\s*(TOOLFORGE_[A-Z0-9_]+)=(.*)$", line)
        if setting:
            found[setting[1]] = (setting[2].strip(), " ".join(comment))
            comment = []
        elif line.startswith("#"):
            comment.append(line.lstrip("#").strip())
    return found


def settings_table() -> str:
    _toolforge()
    from toolforge.config import ENV_PREFIX, Settings  # noqa: PLC0415

    described = _env_example(ROOT / ".env.example")
    rows = []
    for name in sorted(Settings.model_fields):
        env = f"{ENV_PREFIX}{name.upper()}"
        default, comment = described.get(env, ("", ""))
        rows.append(
            [
                th_row(f"<code>{esc(env)}</code>"),
                td(f"<code>{esc(default)}</code>" if default else "unset"),
                td(inline_code(comment) if comment else "Documented in <code>.env.example</code>."),
            ]
        )
    return table(
        "Every setting, with its default", ["Setting", "Default", "What it does"], rows, css="settings"
    )


VERDICT_CLASS = {
    "contained": "v-contained",
    "breached": "v-breached",
    "inconclusive": "v-neutral",
    "skipped": "v-neutral",
}


def attacks_table(results: dict[str, Any]) -> str:
    _toolforge()
    from toolforge.attacks.catalog import ATTACKS  # noqa: PLC0415

    measured = ((results.get("attacks") or {}).get("per_attack")) or {}
    rows = []
    for attack in ATTACKS:
        verdict = measured.get(attack.id)
        cell = (
            f'<span class="verdict {VERDICT_CLASS.get(verdict, "v-neutral")}">{esc(verdict)}</span>'
            if verdict
            else NOT_MEASURED
        )
        rows.append(
            [
                th_row(f"<code>{esc(attack.id)}</code><br>{esc(attack.title)}"),
                td(inline_code(attack.attack)),
                td(inline_code(attack.defence)),
                td(cell, "result"),
            ]
        )
    return table(
        "The Attack Lab: each hostile tool author, the defence, and the latest verdict",
        ["Attack", "What the hostile author does", "What should stop it", "Verdict"],
        rows,
        css="attacks",
    )


def attacks_meta(results: dict[str, Any]) -> str:
    _toolforge()
    from toolforge.attacks.runner import FOOTER  # noqa: PLC0415

    run = results.get("attacks")
    if not run:
        lead = (
            f"{NOT_MEASURED} No Attack Lab result has been recorded for this site. "
            "Run <code>toolforge attacks run --json</code>, then <code>python scripts/build_site_data.py</code>."
        )
    else:
        docker = run.get("docker")
        lead = (
            f"Run <code>{esc(run['run_id'])}</code> on {esc(str(run.get('generated_at') or 'an unrecorded date')[:10])}, "
            f"ToolForge {esc(run.get('toolforge') or 'version not recorded')}"
            f"{', Docker: ' + esc(docker) if docker else ''}. "
            f"Contained: {fig(run['contained'])} of {fig(run['total'])}; breached {fig(run['breached'])}; "
            f"inconclusive {fig(run['inconclusive'])}; skipped {fig(run['skipped'])}."
        )
    return f'<p class="meta">{lead}</p><p class="meta">{esc(FOOTER)}</p>'


def _percent(value: Any) -> str | None:
    return (
        f"{round(value * 100)}%" if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    )


def bench_meta(results: dict[str, Any]) -> str:
    bench = results.get("bench")
    if not bench:
        return (
            f'<p class="meta">{NOT_MEASURED} No benchmark has been run for this site. '
            "It needs Docker and a language model: <code>python -m bench</code>. See <code>docs/BENCHMARK.md</code>.</p>"
        )
    model = esc(bench.get("model") or "model not recorded")
    return (
        f'<p class="meta">Run <code>{esc(bench["run_id"])}</code> on {esc(bench.get("date") or "an unrecorded date")}, model '
        f"<code>{model}</code>, up to {fig(bench['runs'])} runs per configuration. "
        "Small samples have wide intervals: a difference counts only where the intervals do not overlap.</p>"
    )


def bench_table(results: dict[str, Any]) -> str:
    bench = results.get("bench")
    if not bench:
        return f'<div class="nm-block">{NOT_MEASURED}</div>'
    rows = []
    for name, config in bench["configs"].items():
        overall = config["overall"]
        rate, interval = _percent(overall.get("success_rate")), overall.get("wilson_95")
        if rate is None or not interval:
            rate_cell, interval_cell = td(NOT_MEASURED, "result"), td(NOT_MEASURED, "result")
        else:
            rate_cell = td(fig(rate))
            interval_cell = td(f"{fig(_percent(interval[0]))} to {fig(_percent(interval[1]))}")
        rows.append(
            [
                th_row(esc(CONFIG_LABELS.get(name, name))),
                td(fig(overall["runs"])),
                td(fig(overall["successes"])),
                rate_cell,
                interval_cell,
            ]
        )
    header = ["Configuration", "Runs", "Successes", "Success rate", "Wilson interval (95 percent)"]
    return table("Benchmark: tasks finished, with and without ToolForge", header, rows, css="bench")


def context_table(results: dict[str, Any]) -> str:
    context = results.get("context")
    if not context:
        return (
            f'<div class="nm-block">{NOT_MEASURED} No context figures have been recorded. This one needs neither Docker nor a model: '
            "<code>python -m bench context --out runs/bench/context</code>, then <code>python scripts/build_site_data.py</code>.</div>"
        )
    method = esc(context["method"])
    rows = []
    for size, row in sorted(context["sizes"].items(), key=lambda item: int(item[0])):
        meta, direct = row["meta"], row["direct"]
        unit = "tokens" if meta.get("tokens") is not None and direct.get("tokens") is not None else "bytes"
        rows.append(
            [
                th_row(fig(size)),
                td(fig(meta[unit])),
                td(fig(direct[unit])),
                td(esc(unit) + f", {method}"),
            ]
        )
    note = (
        '<p class="meta">These figures come from synthetic tools of the stated sizes, made from templates, and from one way of counting '
        f"(<strong>{method}</strong>). They say nothing about any other system.</p>"
    )
    return (
        table(
            "What the server's answers cost in a model's context, by number of tools in the registry",
            [
                "Tools in the registry",
                "With meta-tools (list, find, describe)",
                "Every tool listed up front",
                "Unit and method",
            ],
            rows,
            css="context",
        )
        + note
    )


def limitations(path: Path = ROOT / "docs" / "KNOWN_LIMITATIONS.md") -> str:
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\* \*\*(?P<t>[^*]+?)\*\* (?P<b>.+)$", raw)
        if match:
            items.append(f"<li><strong>{inline_code(match['t'])}</strong> {inline_code(match['b'])}</li>")
    if not items:
        raise SiteError("docs/KNOWN_LIMITATIONS.md has no lines in the form '* **Title.** Text.'")
    return '<ul class="limits-list">' + "".join(items) + "</ul>"


def quickstart(config: dict[str, Any], repo_url: str, prefix: str = "qs") -> str:
    blocks = []
    counter = 0
    for group in config["quickstart"]:
        lines = [command.replace("{{repo_url}}", repo_url) for command in group["commands"]]
        counter += 1
        code_id = f"{prefix}-{counter}"
        blocks.append(
            f'<div class="qs"><h3>{esc(group["title"])}</h3><p>{inline_code(group["text"])}</p>'
            f'<div class="code-wrap"><pre class="code" id="{code_id}" tabindex="0"><code>{esc(chr(10).join(lines))}</code></pre>'
            f'<button type="button" class="copy" data-copy-target="{code_id}" '
            f'aria-label="Copy the commands for: {esc(group["title"])}">Copy</button></div></div>'
        )
    return "".join(blocks)


def capabilities_table(config: dict[str, Any]) -> str:
    rows = [
        [
            th_row(esc(row["name"])),
            td(esc(row["how"])),
            td(chip(row["chip"])),
            td(inline_code(row["limit"])),
        ]
        for row in config["capabilities"]
    ]
    return table(
        "What an agent can forge, by kind of software",
        ["Kind of tool", "How it works", "Status", "Where it stops"],
        rows,
    )


def features_list(config: dict[str, Any], repo_url: str) -> str:
    cards = []
    for row in config["features"]:
        link = f' <a href="{repo_url}/blob/main/{esc(row["doc"])}">Read more</a>' if row.get("doc") else ""
        cards.append(
            f'<li class="card feature"><h3>{esc(row["name"])} {chip(row["chip"])}</h3><p>{inline_code(row["text"])}{link}</p></li>'
        )
    return '<ul class="grid features">' + "".join(cards) + "</ul>"


# --- The guided tour (P20) ----------------------------------------------------------------------------------------------------

TOUR_MARKUP = re.compile(r"\[\[([a-z_]+)(?:\|([^\]]+))?\]\]|`([^`]+)`")


def _tour_builder() -> Any:
    """`scripts/build_tour_data.py`, which owns the step list, the allowlist and the check of a tour data file."""
    scripts = str(ROOT / "scripts")
    sys.path.insert(0, scripts)
    try:
        import build_tour_data  # noqa: PLC0415
    finally:
        sys.path.remove(scripts)
    return build_tour_data


def _authored(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SiteError(f"{path} cannot be read as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SiteError(f"{path} must hold one JSON object")
    return data


@dataclass
class Tour:
    data: dict[str, Any]
    captions: dict[str, Any]
    glossary: dict[str, Any]


def read_tour(site: Path, warnings: list[str]) -> Tour:
    """The tour's three files, checked. Without `data/tour.json` the invented sample story is used, which says what it is."""
    builder = _tour_builder()
    captions = _authored(site / "src" / "tour_captions.json")
    glossary = _authored(site / "src" / "glossary.json")
    path = site / "data" / "tour.json"
    if path.is_file():
        data = _authored(path)
    else:
        warnings.append("no site/data/tour.json: the tour shows the illustrated example, not a recording")
        try:
            data = builder.illustrative(site / "src" / "tour_illustrative.json")
        except builder.TourDataError as exc:
            raise SiteError(str(exc)) from exc
    if problems := builder.validate(data):
        raise SiteError(f"the tour data is not valid: {problems[0]}. Run python scripts/build_tour_data.py")
    for step in builder.STEP_IDS:
        entry = captions.get(step)
        if not isinstance(entry, dict) or not all(
            entry.get(k) for k in ("title", "simple", "technical", "doc")
        ):
            raise SiteError(
                f"tour_captions.json: step {step} needs a title, a simple text, a technical text and a doc"
            )
        for text in (entry["simple"], entry["technical"]):
            for match in TOUR_MARKUP.finditer(text):
                if match[1] and match[1] not in glossary:
                    raise SiteError(
                        f"tour_captions.json: step {step} names the unknown glossary term {match[1]!r}"
                    )
    return Tour(data, captions, glossary)


def caption_html(text: str, glossary: Mapping[str, Any]) -> str:
    """A caption as the written list shows it: escaped, `code` as <code>, a [[term]] as its plain word."""
    out, last = [], 0
    for match in TOUR_MARKUP.finditer(text):
        out.append(html.escape(text[last : match.start()], quote=False))
        if match[3] is not None:
            out.append(f"<code>{html.escape(match[3], quote=False)}</code>")
        else:
            out.append(html.escape(match[2] or glossary[match[1]]["term"], quote=False))
        last = match.end()
    out.append(html.escape(text[last:], quote=False))
    return "".join(out)


def tour_values(tour: Tour, config: dict[str, Any], repo_url: str) -> dict[str, str]:
    """Everything the tour partial needs. The written list works without JavaScript and uses the Simple captions."""
    data, captions = tour.data, tour.captions
    story, source = data["story"], data["source"]
    caveats = "".join(f"<li>{esc(sentence)}</li>" for sentence in data["caveats"])
    intro = (
        f'<p class="tour-badge tour-badge-{esc(source)}">{esc(data["badge"])}</p>'
        + (f'<ul class="tour-caveats">{caveats}</ul>' if caveats else "")
        + f"<p>The task: {esc(story['task'])}. The tool in this story: <code>{esc(story['tool'])}</code>.</p>"
    )
    progress, items = [], []
    for number, step in enumerate(data["steps"], start=1):
        caption = captions[step["id"]]
        current = ' aria-current="step"' if number == 1 else ""
        progress.append(
            f'<li><button type="button" data-step="{number - 1}" aria-label="Step {number}: {esc(caption["title"])}"'
            f"{current}>{number}</button></li>"
        )
        more = ' <a href="#quickstart">Go to the quick start</a>.' if step["id"] == "your_turn" else ""
        items.append(
            f'<li class="card" data-tour-step="{esc(step["id"])}">'
            f'<h3><span class="step-n">{number}</span> {esc(caption["title"])}</h3>'
            f"<p>{caption_html(caption['simple'], tour.glossary)}{more}</p>"
            f'<p class="meta">{esc(step["note"])}</p></li>'
        )
    used = [
        term
        for term in tour.glossary
        if any(f"[[{term}" in captions[step["id"]]["simple"] for step in data["steps"])
    ]
    words = "".join(
        f"<dt>{esc(tour.glossary[t]['term'])}</dt>"
        f"<dd>{esc(tour.glossary[t]['plain'])} In the shop: {esc(tour.glossary[t]['analogy'])}</dd>"
        for t in used
    )
    first = data["steps"][0]
    return {
        "tour_intro": intro,
        "tour_progress": "".join(progress),
        "tour_first_title": esc(captions[first["id"]]["title"]),
        "tour_first_caption": caption_html(captions[first["id"]]["simple"], tour.glossary),
        "tour_first_note": esc(first["note"]),
        "tour_first_doc": esc(captions[first["id"]]["doc"]),
        "tour_quickstart": quickstart(config, repo_url, prefix="tour-qs"),
        "tour_steps": f'<ol class="grid three steps-grid tour-list">{"".join(items)}</ol>',
        "tour_glossary": f'<details class="tour-words"><summary>Words used in the tour</summary><dl>{words}</dl></details>',
    }


# --- Claims against PROGRESS.md -----------------------------------------------------------------------------------------------


def progress_done(path: Path = ROOT / "PROGRESS.md") -> dict[str, bool]:
    """`{patch id: ticked}` from the table at the top of PROGRESS.md."""
    done: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*\[(?P<s>[ x-])\]\s*\|\s*(?P<id>[PS]\d+[a-z]?)\s*\|", line)
        if match:
            done[match["id"]] = match["s"] == "x"
    return done


def check_claims(config: dict[str, Any], done: dict[str, bool]) -> list[str]:
    """Problems with the status chips: a claim of `built` or `experimental` needs every patch it names ticked in PROGRESS.md."""
    problems = []
    for kind in ("capabilities", "features"):
        for row in config[kind]:
            label = f"{kind[:-1]} {row['name']!r}"
            if row.get("chip") not in CHIPS:
                problems.append(f"{label}: chip must be one of {', '.join(CHIPS)}")
                continue
            if not row.get("patches") and row["chip"] != "planned":
                problems.append(f"{label}: names no patch, so nothing backs a {row['chip']} chip")
            for patch in row.get("patches", []):
                if patch not in done:
                    problems.append(f"{label}: patch {patch} is not in PROGRESS.md")
                elif row["chip"] != "planned" and not done[patch]:
                    problems.append(f"{label}: chip {row['chip']} but {patch} is not done in PROGRESS.md")
    return problems


# --- Inline assets and the template -------------------------------------------------------------------------------------------


def inline_svg(path: Path) -> str:
    if not path.is_file():
        raise SiteError(f"missing SVG asset: {path}")
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^<\?xml[^>]*\?>\s*", "", text)
    text = SHELL_STYLE_COMMENT.sub("", text).strip()
    return re.sub(r'\sxmlns="http://www\.w3\.org/2000/svg"', "", text, count=1)


@dataclass
class Context:
    root: Path
    site: Path
    values: dict[str, str]
    config: dict[str, Any]
    results: dict[str, Any]
    repo_url: str

    def generated(self, name: str) -> str | None:
        makers: dict[str, Callable[[], str]] = {
            "tools_table": tools_table,
            "gates_cards": lambda: gates_cards(self.site / "src"),
            "settings_table": settings_table,
            "attacks_table": lambda: attacks_table(self.results),
            "attacks_meta": lambda: attacks_meta(self.results),
            "bench_table": lambda: bench_table(self.results),
            "bench_meta": lambda: bench_meta(self.results),
            "context_table": lambda: context_table(self.results),
            "limitations": limitations,
            "quickstart": lambda: quickstart(self.config, self.repo_url),
            "capabilities_table": lambda: capabilities_table(self.config),
            "features_list": lambda: features_list(self.config, self.repo_url),
        }
        return makers[name]() if name in makers else None


def expand(text: str, ctx: Context, depth: int = 0) -> str:
    if depth > 4:
        raise SiteError("partials include each other too deeply")

    def replace(match: re.Match[str]) -> str:
        name, arg = match[1], match[2]
        if name == "partial" and arg:
            path = ctx.site / "src" / "partials" / f"{arg}.html"
            if not path.is_file():
                raise SiteError(f"{{{{partial:{arg}}}}} names a file that does not exist: {path}")
            return expand(path.read_text(encoding="utf-8"), ctx, depth + 1)
        if name == "svg" and arg:
            return inline_svg(ctx.site / "src" / "assets" / f"{arg}.svg")
        if name == "icon" and arg:
            return inline_svg(ctx.site / "src" / "assets" / "gates" / f"{arg}.svg")
        if arg is None and name in ctx.values:
            return ctx.values[name]
        if arg is None and (made := ctx.generated(name)) is not None:
            return made
        raise SiteError(f"unknown placeholder {match[0]}")

    return TOKEN.sub(replace, text)


def read_results(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SiteError(f"{path} is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


# --- Social image -------------------------------------------------------------------------------------------------------------


def render_social(out: Path) -> str | None:
    """Render `docs/assets/cover.svg` to a PNG with the converter `scripts/render_cover.py` finds; `None` when there is none."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import render_cover  # noqa: PLC0415

        out.parent.mkdir(parents=True, exist_ok=True)
        return render_cover.render(render_cover.SVG, out)
    except Exception:  # an image that cannot be made must not stop the site
        return None
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def social_meta(site_url: str, has_image: bool, image: str) -> str:
    tags = [
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="ToolForge">',
        '<meta property="og:title" content="{{title}}">',
        '<meta property="og:description" content="{{description}}">',
        f'<meta property="og:url" content="{esc(site_url)}">',
        f'<meta name="twitter:card" content="{"summary_large_image" if has_image else "summary"}">',
        '<meta name="twitter:title" content="{{title}}">',
        '<meta name="twitter:description" content="{{description}}">',
    ]
    if has_image:
        tags += [
            f'<meta property="og:image" content="{esc(site_url + image)}">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta property="og:image:alt" content="ToolForge: a new tool passes the gates and an operator\'s approval before it reaches the registry.">',
        ]
    return "\n".join(tags)


# --- The build ----------------------------------------------------------------------------------------------------------------


@dataclass
class BuildInfo:
    dist: Path
    repo_url: str
    repo_source: str
    social: bool
    warnings: list[str]


def build(
    dist: Path | None = None,
    *,
    site: Path = SITE,
    repo_url: str | None = None,
    release: bool = False,
    results_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    git_remote: Callable[[], str | None] = _git_origin,
    social: bool = True,
    check_progress: bool = True,
) -> BuildInfo:
    environ = os.environ if environ is None else environ
    dist = dist or site / "dist"
    config = json.loads((site / "site.json").read_text(encoding="utf-8"))
    url, source = resolve_repo_url(repo_url, config.get("repo_url"), environ, git_remote)
    warnings: list[str] = []
    if is_placeholder(url):
        if release:
            raise SiteError(
                "refusing a release build: no repository address was found. Pass --repo-url, set repo_url in site/site.json, "
                "run inside GitHub Actions (GITHUB_REPOSITORY) or give the checkout an origin remote."
            )
        warnings.append(
            f"the GitHub button points at the placeholder {url}; pass --repo-url for a real preview"
        )
    if check_progress:
        problems = check_claims(config, progress_done())
        if problems:
            raise SiteError("status chips do not match PROGRESS.md:\n  " + "\n  ".join(problems))

    results = read_results(results_path or site / "data" / "results.json")
    tour = read_tour(site, warnings)
    toolforge = _toolforge()
    site_url = (config.get("site_url") or default_site_url(url)).rstrip("/") + "/"
    generated_on = str(results.get("generated_at") or "date not recorded")[:10]

    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)
    has_image = bool(social and render_social(dist / "assets" / "social.png"))
    if social and not has_image:
        warnings.append(
            "no social image: no SVG converter found (see scripts/render_cover.py); the page uses a plain share card"
        )

    values = {
        "repo_url": esc(url),
        "title": esc(config["title"]),
        "tagline": esc(config["tagline"]),
        "description": esc(config["description"]),
        "version": esc(toolforge.__version__),
        "site_url": esc(site_url),
        "generated_on": esc(generated_on),
    }
    values["social_meta"] = TOKEN.sub(
        lambda m: values.get(m[1], m[0]), social_meta(site_url, has_image, "assets/social.png")
    )
    values.update(tour_values(tour, config, url))
    ctx = Context(ROOT, site, values, config, results, url)

    src = site / "src"
    (dist / "index.html").write_text(
        expand((src / "index.html").read_text(encoding="utf-8"), ctx), encoding="utf-8", newline="\n"
    )
    for name in ("styles.css", "app.js", "tour.css", "tour.js"):
        shutil.copyfile(src / name, dist / name)
    (dist / "assets").mkdir(exist_ok=True)
    shutil.copyfile(src / "assets" / "favicon.svg", dist / "assets" / "favicon.svg")
    (dist / "data").mkdir(exist_ok=True)
    documents = {
        "results.json": results,
        "tour.json": tour.data,
        "tour_captions.json": tour.captions,
        "glossary.json": tour.glossary,
    }
    for name, document in documents.items():
        (dist / "data" / name).write_text(
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    leftover = [
        p
        for p in sorted(dist.rglob("*"))
        if p.suffix in {".html", ".css", ".js", ".json", ".svg"} and "{{" in p.read_text(encoding="utf-8")
    ]
    if leftover:
        raise SiteError(f"a placeholder was left unfilled in {leftover[0].relative_to(dist)}")
    return BuildInfo(dist, url, source, has_image, warnings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the ToolForge website into site/dist.")
    parser.add_argument("--release", action="store_true", help="refuse a placeholder repository address")
    parser.add_argument(
        "--repo-url", default=None, help="https://github.com/<owner>/<name> (an ssh remote also works)"
    )
    parser.add_argument("--dist", type=Path, default=None, help="output directory (default: site/dist)")
    args = parser.parse_args(argv)
    try:
        info = build(args.dist, repo_url=args.repo_url, release=args.release)
    except SiteError as exc:
        print(f"build_site: {exc}", file=sys.stderr)
        return 1
    print(f"build_site: repository address {info.repo_url} (from {info.repo_source})")
    for warning in info.warnings:
        print(f"build_site: warning: {warning}", file=sys.stderr)
    size = sum(p.stat().st_size for p in info.dist.rglob("*") if p.is_file() and p.name != "social.png")
    print(f"build_site: wrote {info.dist} ({size // 1024} KB without the social image)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
