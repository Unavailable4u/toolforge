from tool import ToolError, run


def test_parse_fixed_width_basic():
    content = "001     Alice           123.45\n002     Bob             678.90"
    columns = [
        {"name": "id", "width": 8},
        {"name": "name", "width": 20},
        {"name": "amount", "width": 10},
    ]
    result = run({"content": content, "columns": columns})
    assert result["rows"] == [
        {"id": "001", "name": "Alice", "amount": "123.45"},
        {"id": "002", "name": "Bob", "amount": "678.90"},
    ]


def test_parse_fixed_width_empty():
    result = run({"content": "", "columns": [{"name": "id", "width": 8}]})
    assert result["rows"] == []


def test_parse_fixed_width_trailing_spaces():
    content = "ABC  DEF  "
    columns = [{"name": "col1", "width": 5}, {"name": "col2", "width": 5}]
    result = run({"content": content, "columns": columns})
    assert result["rows"] == [{"col1": "ABC", "col2": "DEF"}]