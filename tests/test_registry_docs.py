"""The documented lifecycle and events are the real ones (the drift habit from docs/guide/01-conventions.md)."""

from __future__ import annotations

import re
from pathlib import Path

from toolforge.registry.lifecycle import EDGES, transitions_markdown

ROOT = Path(__file__).resolve().parent.parent


def test_lifecycle_table_in_the_docs_matches_the_code() -> None:
    text = (ROOT / "docs" / "registry.md").read_text(encoding="utf-8")
    match = re.search(r"<!-- transitions:begin -->\n(.*?)\n<!-- transitions:end -->", text, re.DOTALL)
    assert match, "docs/registry.md must wrap the lifecycle table in transitions:begin / end markers"
    assert match.group(1) == transitions_markdown(), (
        "docs/registry.md is out of date: replace the text between the transitions markers with the output of "
        "toolforge.registry.lifecycle.transitions_markdown()"
    )


def test_every_event_the_registry_returns_is_in_the_event_catalogue() -> None:
    architecture = (ROOT / "docs" / "guide" / "02-architecture.md").read_text(encoding="utf-8")
    section = architecture.split("## Event catalogue", 1)[1].split("\n## ", 1)[0]
    catalogue = set(re.findall(r"`(forge\.[a-z_.]+)`", section))
    missing = {edge.event for edge in EDGES.values()} - catalogue
    assert not missing, f"add to the event catalogue in docs/guide/02-architecture.md: {sorted(missing)}"
