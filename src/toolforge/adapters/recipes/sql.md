# Recipe: a tool that queries a database

This is documentation written by the operator of this ToolForge, not output from a tool. Read `package-format` first; this recipe only
adds what is specific to databases. The runtime image has `sqlite3` (standard library) and `duckdb` (call `forge_runtime_info` for the
exact version). You never list them in `requirements`.

## Choose the engine

- A SQLite file (`.db`, `.sqlite`) or a SQL dump: `sqlite3`.
- CSV or JSON files, or analytics over them (window functions, `GROUP BY` over many rows): `duckdb`.
- You only need one table of a CSV and no SQL from the caller: skip SQL and use `csv`.

## Rules that make a query tool safe

1. **Read-only, by the engine and not by looking at the text.** Looking for the word `SELECT` is not a check. For SQLite open the file
   with `file:<path>?mode=ro&immutable=1` (`uri=True`) and install an authorizer that allows only `SQLITE_SELECT`, `SQLITE_READ`,
   `SQLITE_FUNCTION` and `SQLITE_RECURSIVE`. That refuses INSERT, DDL, PRAGMA, ATTACH and `load_extension` with one rule. For DuckDB load
   the files into `:memory:`, then run `SET enable_external_access=false` and `SET lock_configuration=true` before the caller's SQL, so
   it cannot read other files, write files or change the settings back.
2. **Values go in as parameters** (`?`), never pasted into the SQL text. Take `sql` and `params` as separate inputs.
3. **One statement.** SQLite raises `ProgrammingError` ("one statement at a time") on a second one; turn it into a `ToolError`.
4. **Bound the work.** Cap the rows you return (say 100, at most 500, and report `truncated`), cap each cell (300 characters), and stop a
   runaway query: `connection.set_progress_handler(callback, 1000)` where the callback returns 1 after a step budget. A recursive CTE
   with no end is the standard test.
5. **Make every value JSON.** `bytes` become hex text; `inf` and `nan` become text (JSON cannot carry them); `Decimal` and dates
   become text. A result with NaN is an error in ToolForge.
6. **Report problems the caller can fix as `ToolError`**, with a message that names what to change ("only a SELECT is allowed").

## Inputs and examples

The caller's files arrive through `permissions.inputs: true`, and the tool reads the path it is given (for example `inputs/data.db`).
A manifest example can only carry **text**, so a binary database cannot be an example input: use a `.sql` dump or a CSV in the
example's `files`, and build the binary `.db` inside your tests with `sqlite3` in a temporary folder.

Do not put a `pattern` on the input schema's `sql` or `path` properties. G4 generates inputs from the schema and a schema made only of
patterns gives it nothing to try, so the gate is skipped and the version cannot be approved.

## A complete small package: query a CSV with SQL

Every CSV cell is text, so the tool tells the caller to `CAST` a column before comparing or adding it (`CAST(qty AS INTEGER) > 3`);
without it `'5' > 3` is true for every row in SQLite, because text sorts after numbers.

```json manifest
{
  "name": "csv_query",
  "version": "1.0.0",
  "kind": "stateless",
  "description": "Load a CSV file into an in-memory SQLite table named data (every column is text: CAST before comparing) and run one read-only SELECT on it; returns columns and rows.",
  "when_to_use": "The task gives a CSV file and a question that one SQL SELECT can answer.",
  "entrypoint": "tool.py:run",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "sql": {"type": "string", "minLength": 1, "maxLength": 1000}
    },
    "required": ["path", "sql"],
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "properties": {"columns": {"type": "array", "items": {"type": "string"}}, "rows": {"type": "array", "items": {"type": "array"}}},
    "required": ["columns", "rows"]
  },
  "permissions": {"inputs": true},
  "examples": [
    {
      "files": {"inputs/data.csv": "name,qty\nnut,5\nbolt,2\n"},
      "input": {"path": "inputs/data.csv", "sql": "SELECT name FROM data WHERE CAST(qty AS INTEGER) > 3"},
      "output": {"columns": ["name"], "rows": [["nut"]]}
    }
  ]
}
```

```python tool.py
import csv
import sqlite3

from toolforge_sdk import ToolError

MAX_ROWS = 100
ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _guard(action, arg1, arg2, db_name, source):
    return sqlite3.SQLITE_OK if action in ALLOWED else sqlite3.SQLITE_DENY


def run(args):
    try:
        with open(args["path"], newline="", encoding="utf-8") as handle:
            table = list(csv.reader(handle))
    except OSError:
        raise ToolError("cannot read that file; pass the name of a file the caller provided") from None
    if not table or not all(table[0]):
        raise ToolError("the first CSV line must name every column")
    connection = sqlite3.connect(":memory:")
    names = ", ".join('"' + name.replace('"', '""') + '"' for name in table[0])
    connection.execute(f"CREATE TABLE data ({names})")
    marks = ", ".join("?" * len(table[0]))
    connection.executemany(f"INSERT INTO data VALUES ({marks})", [row for row in table[1:] if len(row) == len(table[0])])
    connection.set_authorizer(_guard)  # from here on only reading is allowed
    try:
        cursor = connection.execute(args["sql"].strip().rstrip(";"))
        rows = cursor.fetchmany(MAX_ROWS)
        columns = [item[0] for item in cursor.description or []]
    except sqlite3.ProgrammingError:
        raise ToolError("give exactly one SELECT statement") from None
    except sqlite3.DatabaseError as error:
        raise ToolError("the query was refused: " + str(error)[:100]) from None
    finally:
        connection.close()
    return {"columns": columns, "rows": [list(row) for row in rows]}
```

```python tests/test_csv_query.py
import os
import tempfile

from tool import ToolError, run


def make(text):
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "data.csv")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def test_select_returns_columns_and_rows():
    path = make("name,qty\nnut,5\nbolt,2\n")
    assert run({"path": path, "sql": "SELECT name, qty FROM data ORDER BY qty"}) == {
        "columns": ["name", "qty"],
        "rows": [["bolt", "2"], ["nut", "5"]],
    }


def test_a_write_is_refused():
    path = make("name\nnut\n")
    try:
        run({"path": path, "sql": "DELETE FROM data"})
    except ToolError as error:
        assert "refused" in str(error)
    else:
        raise AssertionError("expected ToolError")


def test_two_statements_are_refused():
    path = make("name\nnut\n")
    try:
        run({"path": path, "sql": "SELECT 1; SELECT 2"})
    except ToolError as error:
        assert "exactly one" in str(error)
    else:
        raise AssertionError("expected ToolError")
```

The full reference version of this idea (SQLite files and dumps, a step budget, row caps, JSON-safe cells) is the pack `sqlite_query`
that ships in `examples/toolpacks/` of the ToolForge repository.
