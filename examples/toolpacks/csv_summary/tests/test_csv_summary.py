import os
import tempfile

from tool import ToolError, run


def _summarise(text):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "data.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return run({"path": path})


def test_counts_rows_and_lists_columns():
    result = _summarise("a,b\n1,2\n3,4\n")
    assert result["rows"] == 2
    assert result["columns"] == ["a", "b"]


def test_numeric_columns_get_min_max_mean():
    result = _summarise("a,name\n1,x\n3,y\n8,z\n")
    assert result["numeric"] == {"a": {"min": 1.0, "max": 8.0, "mean": 4.0}}


def test_header_only_file_has_zero_rows_and_no_numeric_columns():
    assert _summarise("a,b\n") == {"rows": 0, "columns": ["a", "b"], "numeric": {}}


def test_mixed_column_is_not_numeric():
    assert _summarise("a\n1\nabc\n")["numeric"] == {}


def test_missing_file_is_a_handled_error():
    try:
        run({"path": "no/such/file.csv"})
    except ToolError as error:
        assert "cannot read" in str(error)
    else:
        raise AssertionError("expected ToolError")
