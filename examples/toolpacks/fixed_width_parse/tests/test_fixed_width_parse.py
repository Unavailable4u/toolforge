from tool import ToolError, run

COLUMNS = [
    {"name": "id", "start": 0, "width": 4, "type": "int"},
    {"name": "name", "start": 4, "width": 10},
    {"name": "amount", "start": 14, "width": 8, "type": "float"},
]
TEXT = "ID  NAME      AMOUNT\n0001Ada          12.50\n0002Grace Hop   3.25\n"


def test_cuts_columns_and_converts_types():
    result = run({"text": TEXT, "columns": COLUMNS, "skip_lines": 1})
    assert result["rows"] == [
        {"id": 1, "name": "Ada", "amount": 12.5},
        {"id": 2, "name": "Grace Hop", "amount": 3.25},
    ]
    assert result["row_count"] == 2 and result["errors"] == [] and result["truncated"] is False


def test_a_cell_that_is_not_its_type_is_null_and_listed():
    result = run({"text": "00x1Ada          12.50\n", "columns": COLUMNS})
    assert result["rows"][0]["id"] is None and result["rows"][0]["name"] == "Ada"
    assert result["errors"] == [{"line": 1, "column": "id", "message": "'00x1' is not a int"}]


def test_short_lines_give_what_exists_and_blank_numbers_are_null():
    result = run({"text": "0007Bo\n", "columns": COLUMNS})
    assert result["rows"] == [{"id": 7, "name": "Bo", "amount": None}]


def test_strip_false_keeps_the_padding():
    result = run({"text": "0001Ada       \n", "columns": [COLUMNS[1]], "strip": False})
    assert result["rows"] == [{"name": "Ada       "}]


def test_blank_lines_are_ignored_and_rows_are_capped():
    text = "\n".join(f"{n:04d}x" for n in range(600)) + "\n\n"
    result = run({"text": text, "columns": [COLUMNS[0]]})
    assert result["row_count"] == 500 and result["truncated"] is True and result["rows"][-1] == {"id": 499}


def test_duplicate_column_names_are_refused():
    try:
        run({"text": "x", "columns": [COLUMNS[0], COLUMNS[0]]})
    except ToolError as error:
        assert "different" in str(error)
    else:
        raise AssertionError("expected ToolError")
