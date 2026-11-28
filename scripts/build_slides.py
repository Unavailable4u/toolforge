"""Build the slide outline (Marp Markdown) from facts in the repository, so no number on a slide is typed by hand.

    python scripts/build_slides.py                       # runs/slides/slides.md, with any results found under runs/
    python scripts/build_slides.py --no-results --out docs/slides.md    # the committed copy: results slides say "Not measured yet"
    python scripts/build_slides.py --check               # exit 1 when docs/slides.md is not what --no-results would write

What each slide draws on: the gate list from `GATE_INFO`; the acts from `demo/acts.py`; the demo slide from the committed recording
(`demo/recordings/main`); the benchmark slide from the newest `runs/bench/*/summary.json`; the Attack Lab slide from the newest
`runs/attacks/attacks-*.json`; the limits from `docs/KNOWN_LIMITATIONS.md`. A result file that is missing makes its slide say
"Not measured yet" and name the command that measures it, never a placeholder number.

Render with Marp (`marp docs/slides.md`), or read it as Markdown. Proven by `tests/test_docs_p18.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import acts  # noqa: E402
from toolforge.verify.pipeline import GATE_INFO  # noqa: E402

HEADER = "---\nmarp: true\ntheme: default\npaginate: true\n---\n"
BENCH_HOW = "python -m bench --config all --runs 5 --out runs/bench/<timestamp>/ (docs/BENCHMARK.md)"
ATTACKS_HOW = "toolforge attacks run --json runs/attacks (docs/ATTACKS.md)"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def newest(directory: Path, pattern: str) -> Path | None:
    found = sorted(directory.glob(pattern)) if directory.is_dir() else []
    return found[-1] if found else None


def pct(value: float | None) -> str:
    return "not measured" if value is None else f"{value * 100:.0f}%"


def interval(value: list[float] | None) -> str:
    return "n/a" if not value else f"{value[0] * 100:.0f}% to {value[1] * 100:.0f}%"


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]


def limitation_titles(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return re.findall(r"^\* \*\*(.+?)\.\*\*", text, re.M)


def gate_runs(events: list[dict[str, Any]]) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """act -> tool -> [(gate, status)] in the order the gates finished, from the recording's events."""
    out: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for event in events:
        kind = str(event.get("type", ""))
        gate = (event.get("facts") or {}).get("gate")
        if kind in ("forge.gate.passed", "forge.gate.failed", "forge.gate.skipped") and gate:
            out.setdefault(event.get("act") or "?", {}).setdefault(event.get("tool") or "?", []).append(
                (gate, kind.rsplit(".", 1)[1])
            )
    return out


def compress(steps: list[tuple[str, str]]) -> str:
    """`G1 passed, G2 passed, G3 passed, G4 skipped` -> `G1 to G3 passed, G4 skipped`."""
    groups: list[list[Any]] = []
    for gate, status in steps:
        if groups and groups[-1][2] == status:
            groups[-1][1] = gate
        else:
            groups.append([gate, gate, status])
    return ", ".join((f"{a} {s}" if a == b else f"{a} to {b} {s}") for a, b, s in groups)


def demo_lines(recording_dir: Path) -> list[str]:
    meta = _read_json(recording_dir / "recording.json")
    events = (_read_json(recording_dir / "events.json") or {}).get("events", [])
    if not isinstance(meta, dict) or not events:
        return [
            "No recording is committed yet: run `python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/main/`."
        ]
    runs = gate_runs(events)
    lines = []
    for act in acts.ACTS:
        tools = runs.get(act.id, {})
        what = (
            "; ".join(f"`{tool.split('@')[0]}` {compress(steps)}" for tool, steps in tools.items())
            or "no gate ran (the shelf was used)"
        )
        lines.append(f"- **Act {act.number}, {act.title}:** {what}")
    sandbox = "Docker" if meta.get("isolated") else "the stand-in sandbox (no isolation)"
    agent = "a scripted agent" if meta.get("agent") == "scripted" else "a live model"
    lines.append("")
    lines.append(f"Recorded {meta.get('recorded_at')} with {agent} and {sandbox}; G5 was {meta.get('g5')}.")
    return lines


def bench_lines(runs_dir: Path | None) -> list[str]:
    path = newest(runs_dir / "bench", "*/summary.json") if runs_dir else None
    summary = _read_json(path) if path else None
    if not isinstance(summary, dict) or not isinstance(summary.get("configs"), dict):
        return [f"Not measured yet. Run `{BENCH_HOW}`; this slide fills in from its summary.json."]
    lines = [
        f"Model: {summary.get('model') or 'not recorded'}. Result generated {summary.get('generated_at') or 'at an unrecorded time'}.",
        "",
        "| Configuration | Runs | Successes | Success rate | Wilson 95% | Median tool calls |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, config in summary["configs"].items():
        block = config.get("overall", {})
        calls = block.get("median_tool_calls")
        lines.append(
            f"| {name} | {block.get('runs')} | {block.get('successes')} | {pct(block.get('success_rate'))} | "
            f"{interval(block.get('wilson_95'))} | {'not measured' if calls is None else f'{calls:g}'} |"
        )
    return lines


def attack_lines(runs_dir: Path | None) -> list[str]:
    path = newest(runs_dir / "attacks", "attacks-*.json") if runs_dir else None
    document = _read_json(path) if path else None
    if not isinstance(document, dict):
        return [f"Not measured yet. Run `{ATTACKS_HOW}`; this slide fills in from the result file."]
    from bench.report import attack_lab

    try:
        lab = attack_lab(document)
    except ValueError:
        return ["The attacks result file is not in the format `toolforge attacks run --json` writes."]
    return [
        f"Containment rate: {pct(lab['containment_rate'])} ({interval(lab['wilson_95'])}) over {lab['attacks']} attacks.",
        f"Contained {lab['contained']}, breached {lab['breached']}, inconclusive {lab['inconclusive']}, skipped {lab['skipped']}.",
        f"Docker: {lab['docker'] or 'not recorded'}. Result generated {lab['generated_at'] or 'at an unrecorded time'}.",
    ]


def render(runs_dir: Path | None) -> str:
    """The whole deck. `runs_dir=None` means no results at all (the committed copy)."""
    slides: list[list[str]] = []
    slides.append(
        [
            "# ToolForge",
            "",
            "Agents that build their own tools, and a registry that makes them safe to reuse.",
            "",
            "An MCP server. Sandboxed by MiniLocker. Every step on a tamper-evident ledger.",
        ]
    )
    slides.append(
        [
            "## The problem",
            "",
            "- An agent meets a task it has no tool for, and gives up or runs code it wrote with its own trust.",
            "- That code may have been steered by a hostile page or a poisoned file.",
            "- Whatever it builds is thrown away: the next task starts from nothing.",
        ]
    )
    slides.append(
        [
            "## The idea",
            "",
            "1. **Forge:** the agent submits a tool, or asks the server to build one.",
            "2. **Verify:** the tool is tested, attacked and measured before anyone can use it.",
            "3. **Reuse:** a person promotes it; any agent finds it, runs it in a sandbox, and trusts the record.",
        ]
    )
    slides.append(
        [
            "## How it works",
            "",
            "- MCP server: a small, fixed set of meta tools. Nothing that approves or promotes is reachable from it.",
            "- Registry: immutable versions, one state machine, frozen packages.",
            "- Sandbox: a fresh hardened container per call, no network unless approved.",
            "- Operator: the command line and a dashboard, separate from the agent's port.",
        ]
    )
    gate_rows = ["## The gates", ""] + [
        f"- **{g.id} {g.name}:** {first_sentence(g.plain_summary)}" for g in GATE_INFO
    ]
    slides.append(gate_rows)
    slides.append(
        [
            "## Containment",
            "",
            "- Hardened Docker containers from MiniLocker: read-only root, no capabilities, limits, no route out.",
            "- Decoy credentials are planted; touching one fails the tool.",
            "- A hash-chained ledger and a Blast Radius report say what happened, and what was *not measured*.",
        ]
    )
    slides.append(
        ["## The demo", ""]
        + [f"- **Act {a.number}, {a.title}:** {a.shows}" for a in acts.ACTS]
        + ["", "What the committed recording shows:", ""]
        + demo_lines(ROOT / "demo" / "recordings" / "main")
    )
    slides.append(["## Benchmark", ""] + bench_lines(runs_dir))
    slides.append(["## Attack Lab", ""] + attack_lines(runs_dir))
    titles = limitation_titles(ROOT / "docs" / "KNOWN_LIMITATIONS.md")
    slides.append(
        ["## Limits, said plainly", ""]
        + [f"- {t}" for t in titles[:6]]
        + ["", "All of them: `docs/KNOWN_LIMITATIONS.md`."]
    )
    return HEADER + "\n---\n\n".join("\n".join(s) + "\n" for s in slides)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the slide outline.")
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"))
    parser.add_argument(
        "--no-results", action="store_true", help="ignore any result files (what the committed copy uses)"
    )
    parser.add_argument("--out", default=None)
    parser.add_argument("--check", action="store_true", help="compare docs/slides.md with a no-results build")
    args = parser.parse_args(argv)
    committed = ROOT / "docs" / "slides.md"
    if args.check:
        same = committed.is_file() and committed.read_text(encoding="utf-8") == render(None)
        print(
            "docs/slides.md is up to date"
            if same
            else "docs/slides.md is out of date: python scripts/build_slides.py --no-results --out docs/slides.md"
        )
        return 0 if same else 1
    text = render(None if args.no_results else Path(args.runs_dir))
    out = Path(args.out) if args.out else ROOT / "runs" / "slides" / "slides.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
