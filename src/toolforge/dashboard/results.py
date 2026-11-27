"""Stored attack-lab and benchmark results, read from files (P17).

The dashboard runs neither. Starting sandboxes or model calls from a web request is the wrong trust shape, so `toolforge attacks` and
`python -m bench` (P16) stay command-line tools and this module only reads what they wrote:

* attack runs: `<runs>/attacks/attacks-<time>.json`, which `toolforge attacks run --json` writes (P15; the file is
  `{generated_at, toolforge, docker, summary, footer, results: [{id, title, verdict, evidence, seconds, checks}]}`), and one directory level below;
* benchmark runs: `<runs>/bench/<timestamp>/summary.json`.

`<runs>` is `runs` in the directory the dashboard was started from, or what `--runs-dir` says. A missing directory, an empty one and an
unreadable file are all "no results yet", said as a gap. The newest file by modification time is shown; the other names are listed so the
page can ask for one of them. The name a client asks for is looked up in the listing and never joined to a path. Text in a result file is
cleaned like every other piece of stored text, and a file over 2 MB is reported as too large instead of being read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from toolforge.server.fences import clean_json
from toolforge.textsafety import sanitize

MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_LISTED = 20
KINDS = ("attacks", "bench")


def _candidates(runs_dir: Path, kind: str) -> list[Path]:
    base = runs_dir / kind
    patterns = ("*.json", "*/*.json") if kind == "attacks" else ("*/summary.json",)
    found: list[Path] = []
    for pattern in patterns:
        try:
            found += [p for p in base.glob(pattern) if p.is_file() and not p.is_symlink()]
        except OSError:
            continue
    keyed: list[tuple[float, Path]] = []
    for p in found:
        try:
            keyed.append((p.stat().st_mtime, p))
        except OSError:
            continue
    keyed.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in keyed]


def _name(runs_dir: Path, kind: str, path: Path) -> str:
    return sanitize(path.relative_to(runs_dir / kind).as_posix(), max_chars=160)


def stored_results(runs_dir: Path, kind: str, name: str | None = None) -> dict[str, Any]:
    """`{"available", "kind", "files", "latest"}`. `latest` is the newest result, or the one called `name` from the listing."""
    if kind not in KINDS:
        raise ValueError(f"kind is one of {KINDS}")
    paths = _candidates(runs_dir, kind)
    listing = [{"name": _name(runs_dir, kind, p)} for p in paths[:MAX_LISTED]]
    out: dict[str, Any] = {"kind": kind, "available": False, "files": listing, "latest": None}
    if not paths:
        out["message"] = f"no stored {kind} results under {sanitize(str(runs_dir / kind), max_chars=200)}"
        return out
    chosen = paths[0]
    if name is not None:
        match = next((p for p in paths[:MAX_LISTED] if _name(runs_dir, kind, p) == name), None)
        if match is None:
            out["message"] = "no such result in the listing"
            return out
        chosen = match
    try:
        stat = chosen.stat()
        if stat.st_size > MAX_RESULT_BYTES:
            out["message"] = "the result file is larger than 2 MB and was not read"
            return out
        document = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        out["message"] = "the newest result file could not be read as JSON"
        return out
    out["available"] = True
    out["latest"] = {
        "name": _name(runs_dir, kind, chosen),
        "modified": stat.st_mtime,
        "document": clean_json(document, 600),
    }
    return out
