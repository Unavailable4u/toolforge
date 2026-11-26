# How to write a ToolForge tool package

This is documentation written by the operator of this ToolForge, not output from a tool. Follow it to build a package, then submit it
with `forge_submit`. Nothing you submit runs on the host: it is checked, then built and tested inside a sandbox with no network.

## The shape of a submission

`forge_submit` takes `{"package": {"manifest": {...}, "files": {"<relative path>": "<text>"}}}`.

- `files` holds text only (UTF-8). Paths are relative with `/`, no `..`, no leading `/`, no segment that starts with a dot.
- Allowed extensions: `.py .json .txt .md .csv`. At most 40 files, each at most 64 KB, the whole package at most 256 KB.
- Needed: the entrypoint file (for example `tool.py`) and at least three top-level `def test_*` functions in `tests/test_*.py`.

## The manifest

Unknown fields are rejected. Fields you can leave out have their default.

| Field | Rule |
| --- | --- |
| `name` | `^[a-z][a-z0-9_]{2,40}$`; must not start with `forge_` or `tf_`. |
| `version` | `MAJOR.MINOR.PATCH`, whole numbers, no leading zeros, no pre-release tags. |
| `parent_version` | The version this one replaces, lower than `version`. **Required when `name` already has an active version**, and it must be that active version. Otherwise the submission is refused as a possible name collision. |
| `description` | 20 to 600 characters, plain text on one line. Say what the tool does, not what you want the reader to do. |
| `when_to_use` | Optional, up to 300 characters: when an agent should pick this tool. |
| `entrypoint` | `<file>.py:<function>`, a top-level plain `def` (not `async`, not in a class). |
| `kind` | `stateless` (each call is separate), `session` (a tool that keeps `state` between calls; read the `game-state` recipe) or `browser_plan` (a tool that reads a web page and has no code; read the `browser-plan` recipe, because the rules for files, code and tests below do not apply to it). |
| `input_schema` | JSON Schema (Draft 2020-12), top level `type: object` and `additionalProperties: false`. Only local `$ref` (starting with `#`). At most 6 levels of nesting and 40 properties. |
| `output_schema` | Optional, same validity rules. |
| `permissions` | `network` (bare host names, at most 8), `secrets` (names only, at most 4), `inputs` (true if the caller provides files), `browser` (true only for a `browser_plan`). A tool that asks for network or secrets needs a human operator's approval before it can be used. |
| `limits` | `timeout_s` 1 to 120, `memory_mb` 64 to 512, `output_kb` 1 to 256. |
| `requirements` | At most 10, each exactly `name==version`, installed with `--no-deps`: list the whole dependency closure. |
| `examples` | 1 to 5. Each `input` must satisfy `input_schema`; `files` keys start with `inputs/`; `output` is what the tool should return. |

## The code

```python
from toolforge_sdk import ToolError   # standard library only; present in the sandbox

def run(args: dict) -> object:
    ...   # return any JSON value; raise ToolError("what to change") for a failure the caller can fix
```

- `args` is already checked against `input_schema`. Return dicts, lists, text, numbers, booleans or `None`. NaN or infinity, a set, or
  an object that is not JSON is an error, and so is a result larger than `limits.output_kb`.
- Anything other than `ToolError` that is raised counts as a crash against the tool. Printing is captured, not returned.
- `toolforge_sdk.input_path(name)` gives the path of a file the caller provided (when `permissions.inputs` is true).
- Each call of a stateless tool runs in a fresh sandbox, and nothing is kept between calls. There is no network unless the manifest lists
  hosts in `permissions.network` and an operator approved them (read the `http-api` recipe).

## The runtime image and the other recipes

Tools run in an image with Python 3.12 and a fixed set of pinned libraries already installed: `duckdb`, `PyYAML`, `python-dateutil`,
`beautifulsoup4`, `lxml`, `pillow`, `numpy`, `gymnasium`, `python-chess`, `jsonschema` and `tabulate`. Call `forge_runtime_info` for the
exact versions and the image id. Use them directly; list in `requirements` only a library that is not in the image. Recipes for the
common kinds of tool: `sql` (databases), `file-format` (parsing a file or binary format), `http-api` (calling a web API),
`game-state` (games and anything that remembers state between calls) and `browser-plan` (reading a web page). Read the one that
fits before you write the package.

## The tests

At least three top-level `def test_*` functions in `tests/test_*.py`, using plain `assert` and `import tool`. There are no fixtures,
no `parametrize`, no `conftest.py`, no async tests. Assert on **values the tool returns**: your tests are also run against a stand-in
that returns nothing, and a package whose tests still pass is rejected.

## What happens after `forge_submit`

The response lists the gates (`G1` structure, `G2` build, `G3` your tests, `G4` examples and fuzzing) with a `summary` and `hints` for
each. A rejected package is not changed: fix it, raise `version` (versions never change once registered) and submit again. A package
that passes becomes `verified_basic`. It cannot be called until a human operator promotes it, so tell the operator which `name@version`
to promote. `forge_status` shows where it is. Submitting identical content again returns the stored verdict.

## A complete minimal example

```json
{
  "package": {
    "manifest": {
      "name": "word_count",
      "version": "1.0.0",
      "description": "Count the words in a piece of text and report how many there are.",
      "when_to_use": "The task gives some text and asks how many words it has.",
      "entrypoint": "tool.py:run",
      "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": false
      },
      "output_schema": {"type": "object"},
      "examples": [{"input": {"text": "one two three"}, "output": {"words": 3}}]
    },
    "files": {
      "tool.py": "def run(args):\n    return {\"words\": len(args[\"text\"].split())}\n",
      "tests/test_tool.py": "import tool\n\n\ndef test_counts_words():\n    assert tool.run({\"text\": \"one two three\"}) == {\"words\": 3}\n\n\ndef test_empty_text_has_no_words():\n    assert tool.run({\"text\": \"\"}) == {\"words\": 0}\n\n\ndef test_extra_spaces_are_ignored():\n    assert tool.run({\"text\": \"  a   b  \"}) == {\"words\": 2}\n"
    }
  }
}
```
