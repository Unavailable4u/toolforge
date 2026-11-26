"""Parse fixed-width text files."""

from __future__ import annotations

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


def run(args: dict) -> dict:
    content = args["content"]
    columns = args["columns"]

    lines = content.strip().splitlines()
    rows = []

    for line in lines:
        row = {}
        pos = 0
        for col in columns:
            name = col["name"]
            width = col["width"]
            value = line[pos:pos + width].strip()
            row[name] = value
            pos += width
        rows.append(row)

    return {"rows": rows}