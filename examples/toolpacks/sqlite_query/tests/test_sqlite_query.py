import sqlite3
import tempfile
import os

from tool import ToolError, run


def _run_query(sql, params=None):
    """Create a temporary SQLite database and run the query tool on it."""
    with tempfile.TemporaryDirectory() as folder:
        # Create a test database
        db_path = os.path.join(folder, "products.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE products (id INTEGER, name TEXT, price REAL)")
        cursor.execute("INSERT INTO products VALUES (1, 'Laptop', 999.99)")
        cursor.execute("INSERT INTO products VALUES (2, 'Phone', 599.99)")
        cursor.execute("INSERT INTO products VALUES (3, 'Tablet', 299.99)")
        conn.commit()
        conn.close()

        # Copy to inputs directory
        inputs_dir = os.path.join(folder, "inputs")
        os.makedirs(inputs_dir)
        os.rename(db_path, os.path.join(inputs_dir, "products.db"))

        # Run the tool
        old_cwd = os.getcwd()
        os.chdir(folder)
        try:
            result = run({"query": sql, "params": params or []})
        finally:
            os.chdir(old_cwd)
        return result


def test_select_all_products():
    result = _run_query("SELECT * FROM products")
    assert result["columns"] == ["id", "name", "price"]
    assert result["rows"] == [
        ["1", "Laptop", "999.99"],
        ["2", "Phone", "599.99"],
        ["3", "Tablet", "299.99"],
    ]


def test_select_with_where_clause():
    result = _run_query("SELECT name, price FROM products WHERE price > ?", [500])
    assert result["columns"] == ["name", "price"]
    assert result["rows"] == [
        ["Laptop", "999.99"],
        ["Phone", "599.99"],
    ]


def test_select_with_params():
    result = _run_query("SELECT * FROM products WHERE id = ?", [2])
    assert result["columns"] == ["id", "name", "price"]
    assert result["rows"] == [["2", "Phone", "599.99"]]


def test_missing_database_raises_error():
    import os
    import shutil

    with tempfile.TemporaryDirectory() as folder:
        # No database file at all
        old_cwd = os.getcwd()
        os.chdir(folder)
        try:
            try:
                run({"query": "SELECT * FROM products"})
                assert False, "Expected ToolError"
            except ToolError as e:
                assert "no .db file found in inputs" in str(e)
        finally:
            os.chdir(old_cwd)


def test_multiple_databases_raises_error():
    import os
    import shutil

    with tempfile.TemporaryDirectory() as folder:
        # Create two databases
        for name in ["products.db", "other.db"]:
            db_path = os.path.join(folder, name)
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INT)")
            conn.close()

        inputs_dir = os.path.join(folder, "inputs")
        os.makedirs(inputs_dir)
        for name in ["products.db", "other.db"]:
            os.rename(os.path.join(folder, name), os.path.join(inputs_dir, name))

        old_cwd = os.getcwd()
        os.chdir(folder)
        try:
            try:
                run({"query": "SELECT * FROM products"})
                assert False, "Expected ToolError"
            except ToolError as e:
                assert "multiple .db files found" in str(e)
        finally:
            os.chdir(old_cwd)