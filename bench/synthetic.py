"""A seeded, synthetic registry of N active tools, for the context-cost measurement (`bench/context.py`).

The tools are generated from a few templates with typical manifest sizes (a description of one to three sentences, a `when_to_use`
line, two to four input properties, one example). They are registered straight into a real `Registry` with **scripted gate results**:
G1 to G4 recorded as passed with the detail `"synthetic: scripted"`, G5 and G6 skipped, a stored blob, then moved to `active` by the
`operator` actor. No sandbox, no Docker, no ledger, and nothing here ever runs a tool: `context.py` answers `forge_run` with a stand-in.
This is the fast path the measurement needs (1000 tools register in seconds; the full pipeline would take minutes) and it is not a
claim that these tools were verified.

Determinism: the same `(size, seed)` gives byte-identical packages (`digest()`), and the registry runs on a fixed clock, so every
response the server builds from it is identical too (`tests/test_bench_context.py`). `bench` may not import from `tests/`, so this module
keeps its own small helpers.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toolforge.models.package import ToolPackage
from toolforge.registry import DB_NAME, Registry

FIXED_CLOCK = "2026-01-01T00:00:00Z"
SCRIPTED_GATES = (
    ("G1", "passed"),
    ("G2", "passed"),
    ("G3", "passed"),
    ("G4", "passed"),
    ("G5", "skipped"),
    ("G6", "skipped"),
)

# (verb, noun, description, when_to_use, input properties). Text tools take {"text"} and return {"text", "count"}, so three of
# them can be chained; the others only add realistic variety to the registry.
TEMPLATES: tuple[tuple[str, str, str, str, dict[str, Any]], ...] = (
    (
        "clean",
        "text",
        "Normalise a piece of text: collapse runs of whitespace, strip control characters and trim both ends. "
        "Returns the cleaned text and its number of words.",
        "The task gives free text that needs tidying before it is counted, compared or stored.",
        {"text": {"type": "string"}},
    ),
    (
        "count",
        "words",
        "Count the words in a piece of text, treating any run of letters, digits and apostrophes as one word. "
        "Returns the text unchanged and the count.",
        "The task asks how many words a text has, or needs a word count to compare documents.",
        {"text": {"type": "string"}},
    ),
    (
        "redact",
        "emails",
        "Replace every e-mail address in a text with [email]. Returns the redacted text and how many addresses were replaced.",
        "The task needs text shared without personal e-mail addresses in it.",
        {"text": {"type": "string"}},
    ),
    (
        "summarise",
        "csv",
        "Read a CSV file and report, for each numeric column, the count, minimum, maximum and mean. "
        "Non-numeric columns are listed with their number of distinct values. The first row must be a header.",
        "The task gives a CSV file and asks for quick column statistics.",
        {"path": {"type": "string"}, "delimiter": {"type": "string", "maxLength": 1}},
    ),
    (
        "convert",
        "dates",
        "Convert dates in a list from one written format to ISO 8601, using a stated day/month order for ambiguous dates. "
        "Dates that cannot be read are returned as null with a reason.",
        "The task has dates in mixed or local formats that must be compared or sorted.",
        {"dates": {"type": "array", "items": {"type": "string"}}, "day_first": {"type": "boolean"}},
    ),
    (
        "flatten",
        "json",
        "Flatten nested JSON into dot-separated key paths with their values, optionally limited to a maximum depth. "
        "Lists are indexed by position.",
        "The task needs nested JSON compared, diffed or loaded into a table.",
        {"document": {"type": "object"}, "max_depth": {"type": "integer", "minimum": 1}},
    ),
)
TEXT_TEMPLATES = 3  # the first three templates take and return text
DOMAINS = ("invoices", "tickets", "reviews", "logs", "emails", "notes", "orders", "posts", "reports", "chats")

TOOL_PY = '''"""Synthetic benchmark tool {name}: it exists to be listed and found; it is never run."""


def run(args):
    text = str(args.get("text", ""))
    return {{"text": " ".join(text.split()), "count": len(text.split())}}
'''

TESTS_PY = """import tool


def test_empty():
    assert tool.run({"text": ""})["count"] == 0


def test_two_words():
    assert tool.run({"text": " a  b "})["count"] == 2


def test_cleans():
    assert tool.run({"text": " a  b "})["text"] == "a b"
"""


@dataclass(frozen=True)
class SyntheticTool:
    package: ToolPackage
    template: int

    @property
    def chainable(self) -> bool:
        return self.template < TEXT_TEMPLATES


def tools(size: int, seed: int) -> list[SyntheticTool]:
    """The `size` tools for `seed`, in registration order."""
    if size < 1:
        raise ValueError("a synthetic registry needs at least one tool")
    rng = random.Random(f"toolforge-bench:synthetic:{seed}")
    out = []
    for number in range(size):
        index = number % len(TEMPLATES) if number < len(TEMPLATES) else rng.randrange(len(TEMPLATES))
        verb, noun, description, when, properties = TEMPLATES[index]
        domain = rng.choice(DOMAINS)
        name = f"{verb}_{noun}_{domain}_{number:04d}"
        manifest = {
            "name": name,
            "version": "1.0.0",
            "parent_version": None,
            "description": f"{description} Tuned for {domain}.",
            "when_to_use": when,
            "entrypoint": "tool.py:run",
            "kind": "stateless",
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": [next(iter(properties))],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "count": {"type": "integer"}},
                "required": ["text", "count"],
            },
            "permissions": {"network": [], "secrets": [], "inputs": False, "browser": False},
            "limits": {"timeout_s": 10, "memory_mb": 128, "output_kb": 16},
            "requirements": [],
            "examples": [{"input": _example(properties)}],
        }
        files = {"tool.py": TOOL_PY.format(name=name), "tests/test_tool.py": TESTS_PY}
        out.append(SyntheticTool(ToolPackage(manifest, files), index))
    return out


def _example(properties: dict[str, Any]) -> dict[str, Any]:
    key, schema = next(iter(properties.items()))
    kind = schema.get("type")
    value: Any = {"string": "an example", "array": ["2024-01-02"], "object": {"a": {"b": 1}}}.get(kind, 1)
    return {key: value}


def digest(items: list[SyntheticTool]) -> str:
    """One hash over every package's manifest and files: equal digests mean byte-identical packages."""
    sha = hashlib.sha256()
    for item in items:
        sha.update(json.dumps(item.package.manifest.model_dump(mode="json"), sort_keys=True).encode("utf-8"))
        for path in sorted(item.package.files):
            sha.update(path.encode("utf-8") + b"\0" + item.package.files[path].encode("utf-8"))
    return sha.hexdigest()


def register(registry: Registry, items: list[SyntheticTool]) -> list[str]:
    """Register every tool as active with scripted gate results. Returns the names, in order."""
    names = []
    for item in items:
        record = registry.add_version(item.package, created_by="synthetic")
        registry.attach_blob(record.id, registry.blobs.put(item.package.to_tar()))
        for gate, status in SCRIPTED_GATES:
            registry.record_gate(record.id, gate, status, {"summary": "synthetic: scripted"})
        registry.transition(record.id, "verifying", actor="pipeline", reason="synthetic")
        registry.transition(record.id, "verified_basic", actor="pipeline", reason="synthetic: scripted gates")
        registry.transition(
            record.id, "active", actor="operator", reason="synthetic registry for the context benchmark"
        )
        names.append(record.name)
    return names


def build(home: Path, size: int, seed: int) -> tuple[Registry, list[SyntheticTool]]:
    """A fresh registry at `home` (which must not hold one yet) with `size` synthetic tools, on the fixed clock."""
    if (home / DB_NAME).exists():
        raise FileExistsError(f"{home} already holds a registry")
    registry = Registry(home, clock=lambda: FIXED_CLOCK)
    items = tools(size, seed)
    register(registry, items)
    return registry, items


def chain_names(items: list[SyntheticTool]) -> Iterator[str]:
    """Names of the tools that take and return text, for the three-step chain measurement."""
    return (item.package.manifest.name for item in items if item.chainable)


__all__ = ["FIXED_CLOCK", "SyntheticTool", "build", "chain_names", "digest", "register", "tools"]
