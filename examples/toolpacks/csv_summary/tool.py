"""Summarise a CSV file: row count, column names, and min, max and mean of each numeric column."""

from __future__ import annotations

import csv
import math

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


def _number(cell: str) -> float | None:
    """The cell as a finite number, or None when it is blank or not numeric."""
    try:
        value = float(cell.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def run(args: dict) -> dict:
    try:
        handle = open(args["path"], newline="", encoding="utf-8")
    except (OSError, ValueError):
        raise ToolError(
            f"cannot read {args['path']!r}; pass the name of a file the caller provided"
        ) from None
    with handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise ToolError("the CSV file is empty; it needs at least a header row")
        cells: list[list[str]] = [[] for _ in header]
        rows = 0
        for record in reader:
            if not record:
                continue
            rows += 1
            for index in range(len(header)):
                cells[index].append(record[index] if index < len(record) else "")
    numeric = {}
    for name, column in zip(header, cells, strict=True):
        filled = [cell for cell in column if cell.strip()]
        values = [_number(cell) for cell in filled]
        # A column is numeric only when every non-blank cell is a number.
        if filled and None not in values:
            numeric[name] = {"min": min(values), "max": max(values), "mean": sum(values) / len(values)}
    return {"rows": rows, "columns": header, "numeric": numeric}
