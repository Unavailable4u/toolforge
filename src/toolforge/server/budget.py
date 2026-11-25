"""The size budget of what the MCP server tells a model (P12).

`forge_find` exists so that a model never has to load every tool. This module holds the one constant block that says how much room each
response may take, and `estimate_tokens`. Both are **estimates**: there is no tokenizer in the dependencies, so a token is counted as
four characters (`ceil(len(text) / 4)`). Anything that shows a token figure says "estimated"; P16's benchmark can replace it with counts
from a real endpoint.

The block fixes, in characters of the JSON the model receives:

* `tools_list_chars`: the whole `tools/list` payload with `expose_direct` off. It does not change with the number of tools in the registry.
* `find_chars`: one `forge_find` response at the default `limit` (5). It does not depend on the registry's size either: it has at most five
  results, each with a capped description and a one-line reason.
* `describe_chars`: one `forge_describe` response, signature included. A response that would be larger is cut down, in a fixed order,
  by `fit_describe` and says so (`truncated`), so a tool with a huge manifest cannot fill a model's context.

`tests/test_context_budget.py` registers 10 and then 200 tools and fails when a response is larger than its budget or when the
`tools/list` payload differs. A change that needs more room edits this block **and** adds a line to `docs/guide/DEVIATIONS.md`, so the cost
of a bigger response is a decision and not an accident.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Budget:
    tools_list_chars: int
    find_chars: int
    describe_chars: int


# Measured with the reference tools: tools/list 12.3k (14.7k once P13 added three session tools), a five-result find 1.9k, a typical describe 3.3k. `find` keeps room for the worst case its
# per-field caps allow (eight long hosts on every result); `describe` is a ceiling that `fit_describe` enforces.
BUDGET = Budget(tools_list_chars=15_000, find_chars=9_000, describe_chars=20_000)
CHARS_PER_TOKEN = 4
BASIS = "estimated"  # label for any token figure derived from `estimate_tokens`


def estimate_tokens(text: str) -> int:
    """An estimate: four characters a token, rounded up. Not a tokenizer."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def size_of(value: Any) -> int:
    """The length of the JSON a value is sent as."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def fit_describe(response: dict[str, Any], limit: int = BUDGET.describe_chars) -> dict[str, Any]:
    """Cut a `forge_describe` response down to `limit` characters, in a fixed order, and say so. Returns the same dict.

    Order: fewer versions in the lineage, then fewer manifest examples, then no `output_schema` or examples, then at most two
    hints per gate, then no Blast Radius detail beyond its status. The signature, name, status and manifest's schema are kept as long
    as anything else can go first.
    """
    steps = (
        lambda r: r["lineage"].__setitem__("versions", r["lineage"].get("versions", [])[-5:]),
        lambda r: r["manifest"].__setitem__("examples", r["manifest"].get("examples", [])[:2]),
        lambda r: (r["manifest"].pop("examples", None), r["manifest"].pop("output_schema", None)),
        lambda r: [g.__setitem__("hints", g.get("hints", [])[:2]) for g in r["gates"]],
        lambda r: r.__setitem__("blast_radius", {"status": r["blast_radius"].get("status")}),
    )
    cut = False
    for step in steps:
        if size_of(response) <= limit:
            break
        step(response)
        cut = True
    if cut:
        response["truncated"] = True
        response["budget_note"] = (
            f"the response was cut to fit {limit} characters (estimated {estimate_tokens('x' * limit)} tokens); read what you need with forge_status or forge_list"
        )
    return response


__all__ = ["BASIS", "BUDGET", "Budget", "estimate_tokens", "fit_describe", "size_of"]
