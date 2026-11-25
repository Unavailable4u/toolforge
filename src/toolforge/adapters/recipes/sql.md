# SQL recipe

Use this recipe when the agent needs to query a database.

## Quick example

A tool that queries an SQLite file:

```json
{
  "name": "query_products",
  "description": "Find products by category",
  "kind": "sql",
  "manifest": {
    "database_type": "sqlite",
    "inputs": [
      {
        "name": "database",
        "description": "SQLite database file",
        "path": "products.db"
      }
    ]
  },
  "tool": {
    "input_schema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Product category to search"
        },
        "limit": {
          "type": "integer",
          "description": "Maximum rows to return",
          "default": 100
        }
      },
      "required": ["category"]
    },
    "query": "SELECT id, name, price FROM products WHERE category = :category LIMIT :limit",
    "parameters": {
      "category": "{{category}}",
      "limit": "{{limit}}"
    }
  },
  "test": {
    "fixture": "products.db",
    "queries": [
      {
        "input": {"category": "electronics", "limit": 10},
        "expected_columns": ["id", "name", "price"],
        "max_rows": 10
      }
    ]
  }
}
```

## Constraints

* Databases are opened **read‑only**; no `INSERT`, `UPDATE`, `DELETE` or schema changes.
* Row cap: 10 000 rows per query.
* Maximum result size: 64 MB.
* Parameterised queries only (no string interpolation).
* Only `SELECT` statements; subqueries and CTEs are allowed but must be `SELECT`.
* Each query may use at most 100 ms CPU and 256 MB RAM.

## Database types

* **SQLite**: Single `.db` or `.sqlite` file under `inputs/`.
* **DuckDB**: Single `.duckdb` file under `inputs/`.

The runtime image contains both `sqlite3` and `duckdb` Python modules.

## Writing tests

1. Create a fixture database with representative data.
2. For each test query, list the expected column names (order matters).
3. Optionally provide a list of expected rows (as arrays of values).
4. If you only check shape, set `expected_rows: null` and the test runner will verify the query runs without error.

## Error handling

Failed queries produce a `ToolError` with code `"sql_error"`. The error message is sanitised (no database paths, no schema details beyond column names).

## Performance

Queries that exceed the time or memory limit are terminated and raise `ToolError` with code `"timeout"` or `"memory_limit"`.

## Security notes

* Database paths are always inside the `inputs/` directory.
* The sandbox prevents network connections, so remote databases are not possible.
* Parameters are properly escaped; SQL injection is blocked at the API level.