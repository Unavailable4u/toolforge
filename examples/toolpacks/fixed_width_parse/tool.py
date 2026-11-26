"""Cut fixed-width text into records, using column positions the caller gives (a spec from the task, not guessed).

Each column is `name`, `start` (0-based character offset), `width` (characters) and `type` (`string`, `int` or `float`; default
`string`). Lines shorter than a column's end give the part that exists; a cell that cannot be read as its type becomes `null` and
is listed in `errors` (first 20 only), so one bad line does not hide the rest.
"""

from __future__ import annotations

import math

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


MAX_ROWS = 500
MAX_ERRORS = 20
MAX_CELL_CHARS = 200


def _convert(text: str, kind: str):
    if kind == "string":
        return text[:MAX_CELL_CHARS]
    if text == "":
        return None
    if kind == "int":
        return int(text)
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("not finite")
    return value


def run(args: dict) -> dict:
    columns = args["columns"]
    names = [column["name"] for column in columns]
    if len(set(names)) != len(names):
        raise ToolError("column names must be different from each other")
    skip = args.get("skip_lines", 0)
    strip = args.get("strip", True)
    lines = args["text"].splitlines()[skip:]
    lines = [line for line in lines if line.strip()]
    rows, errors = [], []
    for number, line in enumerate(lines[:MAX_ROWS], start=skip + 1):
        row = {}
        for column in columns:
            start, width = column["start"], column["width"]
            cell = line[start : start + width]
            if strip:
                cell = cell.strip()
            try:
                row[column["name"]] = _convert(cell, column.get("type", "string"))
            except (ValueError, OverflowError):
                row[column["name"]] = None
                if len(errors) < MAX_ERRORS:
                    errors.append(
                        {
                            "line": number,
                            "column": column["name"],
                            "message": f"{cell[:30]!r} is not a {column.get('type')}",
                        }
                    )
        rows.append(row)
    return {
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(lines) > MAX_ROWS,
        "errors": errors,
    }
