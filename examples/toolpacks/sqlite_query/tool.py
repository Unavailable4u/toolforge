"""Run one read-only SELECT against a database the caller provided, and return the column names and rows.

The database is either a SQLite file (`.db`, `.sqlite`, `.sqlite3`), opened read-only, or a text SQL dump (`.sql`) that is loaded into an
in-memory database first (a manifest example can only carry text, so the dump is how a small database is shown and tested).

Safety rules, all enforced by SQLite's own authorizer rather than by looking at the text of the query:
only SELECT may run (no INSERT, UPDATE, DELETE, DDL, PRAGMA, ATTACH, DETACH or VACUUM), the connection is read-only, values go through
`?` placeholders, a query that runs too long is stopped, and the rows returned are capped.
"""

from __future__ import annotations

import math
import sqlite3

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


MAX_DUMP_BYTES = 200_000
MAX_CELL_CHARS = 300
MAX_STEPS = 5_000_000  # SQLite virtual-machine steps; a runaway query is stopped here
_FILE_SUFFIXES = (".db", ".sqlite", ".sqlite3")

# While a dump loads, anything that reaches outside the database is refused; building tables and rows is allowed.
_LOAD_DENIED = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
_LOAD_PRAGMAS = {"foreign_keys"}  # the one a `sqlite3 .dump` file starts with; every other PRAGMA is refused
# While a query runs, only reading is allowed.
_QUERY_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}
_BLOCKED_FUNCTIONS = {"load_extension", "readfile", "writefile"}


def _load_authorizer(action, arg1, arg2, db_name, source):
    if action in _LOAD_DENIED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and str(arg1 or "").lower() not in _LOAD_PRAGMAS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _query_authorizer(action, arg1, arg2, db_name, source):
    if action not in _QUERY_ALLOWED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(arg2 or "").lower() in _BLOCKED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _open(path: str) -> sqlite3.Connection:
    lowered = path.lower()
    if lowered.endswith(".sql"):
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_DUMP_BYTES + 1)
        except OSError:
            raise ToolError(f"cannot read {path!r}; pass the name of a file the caller provided") from None
        if len(raw) > MAX_DUMP_BYTES:
            raise ToolError(
                f"the SQL dump is larger than {MAX_DUMP_BYTES // 1000} KB; provide a database file instead"
            )
        try:
            script = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError("the SQL dump is not UTF-8 text") from None
        connection = sqlite3.connect(":memory:")
        connection.set_authorizer(_load_authorizer)
        try:
            connection.executescript(script)
        except sqlite3.Error as error:
            connection.close()
            raise ToolError(f"the SQL dump could not be loaded: {str(error)[:120]}") from None
        return connection
    if not lowered.endswith(_FILE_SUFFIXES):
        raise ToolError("the file must end in .db, .sqlite, .sqlite3 (a database) or .sql (a text dump)")
    try:
        with open(path, "rb"):
            pass
    except OSError:
        raise ToolError(f"cannot read {path!r}; pass the name of a file the caller provided") from None
    # `mode=ro` and `immutable=1`: the file is never written, not even a journal. The path is quoted for the URI by hand.
    quoted = path.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    try:
        return sqlite3.connect(f"file:{quoted}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        raise ToolError("the file could not be opened as a SQLite database") from None


def _cell(value):
    """A value as JSON: text and numbers as they are, bytes as hex, a number JSON cannot carry as text."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return "0x" + value[: MAX_CELL_CHARS // 2].hex() + ("..." if len(value) > MAX_CELL_CHARS // 2 else "")
    text = str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "..."


def run(args: dict) -> dict:
    sql = args["sql"].strip().rstrip(";").strip()
    if not sql:
        raise ToolError("sql is empty; give one SELECT statement")
    params = list(args.get("params", []))
    max_rows = args.get("max_rows", 100)
    connection = _open(args["path"])
    try:
        steps = [0]

        def watchdog() -> int:
            steps[0] += 1000
            return 1 if steps[0] > MAX_STEPS else 0

        connection.set_authorizer(_query_authorizer)
        connection.set_progress_handler(watchdog, 1000)
        try:
            cursor = connection.execute(sql, params)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(max_rows + 1)
        except (sqlite3.Warning, sqlite3.ProgrammingError) as error:
            if isinstance(error, sqlite3.Warning) or "one statement" in str(error):
                raise ToolError("give exactly one SELECT statement") from None
            raise ToolError(f"the query or its parameters were refused: {str(error)[:120]}") from None
        except sqlite3.DatabaseError as error:
            message = str(error)
            if "not authorized" in message:
                raise ToolError(
                    "only a SELECT that reads tables is allowed (no writes, PRAGMA, ATTACH or DDL)"
                ) from None
            if "interrupted" in message:
                raise ToolError(
                    "the query ran too long and was stopped; narrow it with WHERE or LIMIT"
                ) from None
            raise ToolError(f"the query failed: {message[:120]}") from None
        except (sqlite3.InterfaceError, OverflowError) as error:
            raise ToolError(f"the query or its parameters were refused: {str(error)[:120]}") from None
    finally:
        connection.close()
    truncated = len(rows) > max_rows
    shown = rows[:max_rows]
    return {
        "columns": columns,
        "rows": [[_cell(value) for value in row] for row in shown],
        "row_count": len(shown),
        "truncated": truncated,
    }
