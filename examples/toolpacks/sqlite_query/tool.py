"""Example SQLite query tool demonstrating the SQL recipe."""

from __future__ import annotations

import sqlite3
from pathlib import Path

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


def run(args: dict) -> dict:
    query = args["query"]
    params = args.get("params", [])

    # Find the input database file
    inputs_dir = Path("inputs")
    if not inputs_dir.exists():
        raise ToolError("inputs directory not found")

    db_files = list(inputs_dir.glob("*.db"))
    if not db_files:
        raise ToolError("no .db file found in inputs")
    if len(db_files) > 1:
        raise ToolError(f"multiple .db files found: {[f.name for f in db_files]}")

    db_path = db_files[0]

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        columns = [description[0] for description in cursor.description] if cursor.description else []
    finally:
        conn.close()

    # Convert all values to strings for output
    string_rows = [[str(cell) for cell in row] for row in rows]

    return {"columns": columns, "rows": string_rows}