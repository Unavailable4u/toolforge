import os
import sqlite3
import tempfile

from tool import ToolError, run

SCRIPT = """
CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, price REAL);
INSERT INTO items VALUES (1, 'pen', 1.5), (2, 'book', 12.0), (3, 'lamp', 30.25);
"""


def _folder():
    return tempfile.TemporaryDirectory()


def _db(folder, kind="sql"):
    if kind == "sql":
        path = os.path.join(folder, "shop.sql")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(SCRIPT)
        return path
    path = os.path.join(folder, "shop.db")
    connection = sqlite3.connect(path)
    connection.executescript(SCRIPT)
    connection.commit()
    connection.close()
    return path


def _refused(args, needle):
    try:
        run(args)
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def test_select_from_a_sql_dump_returns_columns_and_rows():
    with _folder() as folder:
        result = run(
            {
                "path": _db(folder),
                "sql": "SELECT name, price FROM items WHERE price > ? ORDER BY price",
                "params": [5],
            }
        )
    assert result == {
        "columns": ["name", "price"],
        "rows": [["book", 12.0], ["lamp", 30.25]],
        "row_count": 2,
        "truncated": False,
    }


def test_select_from_a_real_database_file():
    with _folder() as folder:
        result = run({"path": _db(folder, "db"), "sql": "SELECT count(*) AS n FROM items;"})
    assert result["columns"] == ["n"] and result["rows"] == [[3]]


def test_rows_are_capped_and_the_cap_is_reported():
    with _folder() as folder:
        result = run({"path": _db(folder), "sql": "SELECT id FROM items ORDER BY id", "max_rows": 2})
    assert result["rows"] == [[1], [2]] and result["row_count"] == 2 and result["truncated"] is True


def test_writes_are_refused_and_nothing_changes():
    with _folder() as folder:
        path = _db(folder, "db")
        for sql in (
            "DELETE FROM items",
            "INSERT INTO items VALUES (9, 'x', 1)",
            "DROP TABLE items",
            "UPDATE items SET price = 0",
        ):
            _refused({"path": path, "sql": sql}, "only a SELECT")
        assert run({"path": path, "sql": "SELECT count(*) FROM items"})["rows"] == [[3]]


def test_attach_pragma_and_second_statements_are_refused():
    with _folder() as folder:
        path = _db(folder)
        outside = os.path.join(folder, "other.db")
        _refused({"path": path, "sql": f"ATTACH DATABASE '{outside}' AS other"}, "only a SELECT")
        _refused({"path": path, "sql": "PRAGMA table_info(items)"}, "only a SELECT")
        _refused({"path": path, "sql": "SELECT 1; DELETE FROM items"}, "exactly one SELECT")
        assert not os.path.exists(outside)


def test_a_dump_that_attaches_a_file_does_not_load():
    with _folder() as folder:
        outside = os.path.join(folder, "out.db")
        path = os.path.join(folder, "evil.sql")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"ATTACH DATABASE '{outside}' AS x; CREATE TABLE x.t (a);")
        _refused({"path": path, "sql": "SELECT 1"}, "could not be loaded")
        assert not os.path.exists(outside)


def test_an_unknown_file_kind_and_a_missing_file_are_handled_errors():
    _refused({"path": "inputs/notes.txt", "sql": "SELECT 1"}, "must end in")
    _refused({"path": "no/such/file.db", "sql": "SELECT 1"}, "cannot read")


def test_a_runaway_query_is_stopped():
    with _folder() as folder:
        sql = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT count(*) FROM c"
        _refused({"path": _db(folder), "sql": sql}, "ran too long")


def test_values_that_json_cannot_carry_become_text():
    with _folder() as folder:
        result = run({"path": _db(folder), "sql": "SELECT x'ff00', 1e999, NULL"})
    assert result["rows"] == [["0xff00", "inf", None]]


def test_a_dump_in_the_style_of_sqlite3_dot_dump_loads():
    with _folder() as folder:
        path = os.path.join(folder, "dump.sql")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\nCREATE TABLE t (a INTEGER);\nINSERT INTO t VALUES (7);\nCOMMIT;\n"
            )
        assert run({"path": path, "sql": "SELECT a FROM t"})["rows"] == [[7]]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("PRAGMA writable_schema=ON;\nCREATE TABLE t (a);")
        _refused({"path": path, "sql": "SELECT 1"}, "could not be loaded")
