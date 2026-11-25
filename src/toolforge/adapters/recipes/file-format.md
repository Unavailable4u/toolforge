# File format recipe

Use this recipe when the agent needs to parse or convert an unfamiliar file format.

## Quick example

A tool that parses a fixed‑width text file:

```json
{
  "name": "parse_payroll",
  "description": "Extract fields from a fixed‑width payroll file",
  "kind": "file-format",
  "manifest": {
    "inputs": [
      {
        "name": "payroll_file",
        "description": "Fixed‑width payroll data",
        "path": "payroll.dat"
      }
    ]
  },
  "tool": {
    "input_schema": {
      "type": "object",
      "properties": {
        "start_line": {
          "type": "integer",
          "description": "Line number to start from (1‑based)",
          "default": 1
        },
        "max_lines": {
          "type": "integer",
          "description": "Maximum lines to process",
          "default": 1000
        }
      }
    },
    "format_spec": {
      "type": "fixed_width",
      "columns": [
        {"name": "employee_id", "start": 0, "width": 10},
        {"name": "name", "start": 10, "width": 30},
        {"name": "department", "start": 40, "width": 20},
        {"name": "salary", "start": 60, "width": 12, "type": "decimal"}
      ],
      "encoding": "utf-8",
      "line_terminator": "\\n"
    }
  },
  "test": {
    "fixture": "payroll.dat",
    "expected": [
      {
        "input": {"start_line": 1, "max_lines": 3},
        "output_format": "json",
        "sample_rows": 3
      }
    ]
  }
}
```

## Supported formats

* **Fixed‑width text**: Define column positions and widths.
* **Delimited text (CSV, TSV, etc.)**: Specify delimiter, quote character, and header row.
* **JSON**: Extract specific paths or validate structure.
* **YAML**: Parse YAML documents.
* **Binary formats**: Use a byte‑level specification with field offsets and sizes.
* **TLV (Tag‑Length‑Value)**: A made‑up binary format example for testing.

## Format specification

The `format_spec` object describes how to parse the file. It must include:

1. `type`: One of the format types above.
2. Fields specific to that type (columns, delimiter, byte offsets, etc.).
3. Optional `encoding` (default `"utf-8"`).
4. Optional `skip_lines` or `header` settings.

## Output

The tool returns parsed data as JSON. You can choose:

* `"output_format": "json"` – structured objects.
* `"output_format": "csv"` – delimited text.
* `"output_format": "table"` – markdown table.

## Constraints

* Maximum input size: 64 MB.
* Maximum output size: 64 MB.
* Time limit: 5 seconds.
* The runtime image contains `pandas`, `numpy`, `pyyaml`, and `lxml` for common parsing tasks.

## Writing tests

1. Create a small fixture file that demonstrates the format.
2. For each test case, specify input parameters.
3. Either provide expected output data or use `"sample_rows": N` to verify the parser runs and produces N rows.

## Error handling

Parsing errors produce a `ToolError` with code `"parse_error"`. The message includes the line/byte position where parsing failed.

## Security notes

* Files are read from the `inputs/` directory only.
* The parser runs in a memory‑limited sandbox.
* Binary specifications are checked for buffer‑overflow safety (offsets must stay within file bounds).