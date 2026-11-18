# P02: Tool package spec and validation

Depends on: P01.

## Goal

Define exactly what a tool is, and validate it without running it. A package is a manifest plus source files. Everything
later (registry, pipeline, runner, MCP, builder) speaks this type.

## Scope

`src/toolforge/models/package.py`, `models/results.py` (Issue, ValidationReport), `src/toolforge/packaging.py` (load/dump/tar),
`tests/test_package_*.py`, `examples/toolpacks/word_count/` and `examples/toolpacks/csv_summary/` (two correct reference
packs), `tests/test_examples_validate.py`, `docs/guide/package-format.md` (a user-facing description, the seed of the
`package-format` recipe that P07 publishes), and two CLI commands with `tests/test_cli_package.py`:

* `toolforge validate <dir> [--json]` prints errors and warnings with their hints, exits 1 on any error, and never runs package code.
* `toolforge new <name> [--dir .]` scaffolds a valid package: a manifest, an entrypoint and three real tests that constrain
  behaviour (so the package will also pass G3's test-strength check in P06), then prints the next command to run.

## The package

Layout on disk and in submissions (`files` is `{relative_path: text}` over MCP):

```
manifest.json
tool.py                 # entrypoint module (any module path allowed, see entrypoint)
tests/test_*.py         # at least three test functions across the files
(other .py / .json / .txt / .md / .csv files, small)
```

Manifest (pydantic model `Manifest`):

```json
{
  "name": "csv_summary",
  "version": "1.0.0",
  "parent_version": null,
  "description": "Summarise a CSV file: row count, column names, per-column min, max and mean for numeric columns.",
  "when_to_use": "The task gives a CSV and asks for statistics about it.",
  "entrypoint": "tool.py:run",
  "kind": "stateless",
  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": false},
  "output_schema": {"type": "object"},
  "permissions": {"network": [], "secrets": [], "inputs": true, "browser": false},
  "limits": {"timeout_s": 20, "memory_mb": 256, "output_kb": 64},
  "requirements": [],
  "examples": [{"input": {"path": "inputs/data.csv"}, "files": {"inputs/data.csv": "a,b\n1,2\n3,4\n"}, "output": {"rows": 2}, "live": false}]
}
```

Rules (each becomes an `Issue(path, code, message, hint)` with a stable `code`):

* `name`: `^[a-z][a-z0-9_]{2,40}$`; must not start with `forge_` or `tf_`. `version`: semver `MAJOR.MINOR.PATCH`. `parent_version`: optional semver; it names the version this one replaces and must be lower than `version` (whether it equals the active version is checked at submission time, P07 and P12).
* `description`: 20 to 600 chars after stripping; `when_to_use`: up to 300. Both plain text, no control characters.
* `entrypoint`: `<relative .py path>:<function>`; the file must exist in the package and parse; the function must be defined at module top level (AST check).
* `kind`: `stateless` or `session` (sessions arrive in P13; accept the value now and reject execution later until then).
* `input_schema`: valid JSON Schema (Draft 2020-12 via `jsonschema`), top-level `type: object`, `additionalProperties: false` required, no remote `$ref`s, nesting depth at most 6, at most 40 properties.
* `output_schema`: optional, same validity rules.
* `permissions.network`: list of at most 8 bare hostnames (lower case, no scheme, no port, no wildcard, no IP literal). `secrets`: at most 4 names matching `^[a-z][a-z0-9_]{0,23}$`. `inputs`: whether the tool receives caller files. `browser`: reserved, must be false until P14.
* `limits`: `timeout_s` 1 to 120, `memory_mb` 64 to 512, `output_kb` 1 to 256; unset values take the settings defaults at run time.
* `requirements`: at most 10, each exactly `name==version` (PEP 508 name, plain version). No URLs, VCS references, paths, extras, markers or options. These are installed with `--no-deps`, so the author lists the whole closure.
* `examples`: 1 to 5. Every `input` must validate against `input_schema`. `files` entries are relative paths under `inputs/`. If `output` is given it must be valid against `output_schema` when that exists. `live` defaults to false.
* Files: at most 40, each at most 64 KB, total at most `max_package_kb`. Allowed extensions `.py .json .txt .md .csv`. Paths are relative, normalised, and contain no `..`, no leading `/`, no backslashes, and no segment that starts with a dot. Text only, valid UTF-8, no NUL bytes.
* Tests: at least three top-level `def test_*` functions across `tests/test_*.py` (AST count). Running them is P06's job.

## API

```python
class ToolPackage:
    manifest: Manifest
    files: dict[str, str]
    @classmethod
    def from_dir(cls, path) -> "ToolPackage"
    @classmethod
    def from_submission(cls, payload: dict) -> "ToolPackage"   # {"manifest": {...}, "files": {...}}
    def validate(self, settings) -> ValidationReport           # never raises on bad content
    def to_tar(self) -> bytes                                  # deterministic: sorted names, mtime 0, uid/gid 0, mode 0644
    @property
    def code_sha(self) -> str                                  # sha256 of the deterministic tar
```

`ValidationReport` has `ok`, `issues` (errors) and `warnings`. Parse failures (invalid JSON, wrong types) are issues, not exceptions.

## Tests

Table-driven: one test per rule above with a valid base package mutated to break exactly that rule and an assertion on the
`code`. Determinism of `to_tar`/`code_sha` (same content, different dict order, same hash; one changed byte, different hash).
`from_dir` and `from_submission` agree. Both example packs validate.

## Acceptance

`pytest tests/test_package_*.py tests/test_examples_validate.py tests/test_cli_package.py -q` passes; `examples/toolpacks/*` validate
with no errors; `toolforge new demo_tool --dir /tmp/x && toolforge validate /tmp/x/demo_tool` exits 0.

## Pitfalls

Do not execute any package code here, not even to import it. Use `ast` only. Keep issue messages written for an agent
(see conventions).
