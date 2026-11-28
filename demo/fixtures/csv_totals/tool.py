"""Total a whole-number column of a CSV file for each value of another column (a group-by sum).

Written for the demo's third act (docs/DEMO_SCRIPT.md): the honest tool an agent forges after its hostile attempt was rejected.
Every cell is read as data. A cell that is text, a sentence or an instruction is just a string and is never interpreted.
"""

from __future__ import annotations

import csv

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


MAX_ROWS = 200_000


def run(args: dict) -> dict:
    path, group, column = args["path"], args["group_by"], args["sum_column"]
    try:
        handle = open(path, newline="", encoding="utf-8")
    except (OSError, ValueError):
        raise ToolError(f"cannot read {path!r}; pass the name of a file the caller provided") from None
    totals: dict[str, int] = {}
    rows = 0
    with handle:
        reader = csv.DictReader(handle)
        names = list(reader.fieldnames or [])
        for needed in (group, column):
            if needed not in names:
                raise ToolError(f"the file has no column {needed!r}; its columns are {names}")
        for record in reader:
            rows += 1
            if rows > MAX_ROWS:
                raise ToolError(f"the file has more than {MAX_ROWS} rows; split it first")
            cell = (record.get(column) or "").strip()
            try:
                value = int(cell)
            except ValueError:
                raise ToolError(
                    f"row {rows + 1}: {cell[:40]!r} in column {column!r} is not a whole number"
                ) from None
            key = record.get(group) or ""
            totals[key] = totals.get(key, 0) + value
    return {"rows": rows, "totals": dict(sorted(totals.items()))}
