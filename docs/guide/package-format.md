# Tool package format

A tool is a **package**: one `manifest.json` plus a few small source files. Everything else in ToolForge (the registry, the
verification pipeline, the runner, the MCP server, the builder) works with this one shape. This page is the reference. It is
written for people and for agents, and `toolforge validate` checks every rule below without running your code.

## Layout

On disk it is a folder. Over MCP it is `{"manifest": {...}, "files": {"<relative path>": "<text>"}}`.

```
manifest.json
tool.py                 # the entrypoint module (any .py path is allowed, see `entrypoint`)
tests/test_*.py         # at least three top-level `def test_*` functions across these files
(other .py / .json / .txt / .md / .csv files, small)
```

Files hold text only: valid UTF-8, no NUL characters. Paths are relative, use `/`, contain no `..`, no leading `/`, no backslash,
and no segment starting with a dot. Allowed extensions: `.py .json .txt .md .csv`. At most 40 files, each at most 64 KB, and the
whole package within `TOOLFORGE_MAX_PACKAGE_KB` (default 256). Symbolic links are refused. Folders such as `__pycache__` and
`.pytest_cache` that appear when you run tests are skipped when a folder is read.

## The manifest

```json
{
  "name": "csv_summary",
  "version": "1.0.0",
  "parent_version": null,
  "description": "Summarise a CSV file: row count, column names, and the minimum, maximum and mean of each numeric column.",
  "when_to_use": "The task gives a CSV file and asks for statistics about it.",
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

Unknown fields are rejected, so a typo is an error and not a silently ignored setting.

| Field | Rule |
| --- | --- |
| `name` | `^[a-z][a-z0-9_]{2,40}$`; must not start with `forge_` or `tf_` (those belong to ToolForge). |
| `version` | `MAJOR.MINOR.PATCH`: three whole numbers, no leading zeros, at most six digits each. No pre-release tags. |
| `parent_version` | Optional, same form. The version this one replaces; it must be lower than `version`. Whether it is the currently active version is checked when the package is submitted (a later patch). |
| `description` | 20 to 600 characters after stripping. Plain text on one line: no control, invisible or line-separator characters. |
| `when_to_use` | Optional, up to 300 characters, same text rules. Tells an agent when to pick this tool. |
| `entrypoint` | `<relative .py path>:<function>`. The file must be in the package and parse, and the function must be defined at module top level with plain `def` (not `async`, not inside a class or an `if`, not an assignment). It is found by reading the syntax tree. |
| `kind` | `stateless` or `session`. Sessions are not built yet: the value is accepted now and execution is refused until they exist (a later patch). |
| `input_schema` | A valid JSON Schema (Draft 2020-12). Top level `type: object` and `additionalProperties: false`. No remote `$ref` (every reference starts with `#`) and every local reference must resolve. At most 6 levels of nested subschemas and 40 properties in total. |
| `output_schema` | Optional. Same validity rules (it does not need the two top-level requirements). |
| `permissions.network` | At most 8 bare host names: lower case, no scheme, port, path or wildcard, no IP address. Empty means no network. |
| `permissions.secrets` | At most 4 names matching `^[a-z][a-z0-9_]{0,23}$`. Names only, never values. |
| `permissions.inputs` | Whether the tool receives files the caller provides. |
| `permissions.browser` | Reserved. Must be `false`. |
| `limits.timeout_s` | 1 to 120 seconds. Unset means the setting default applies at run time. |
| `limits.memory_mb` | 64 to 512. |
| `limits.output_kb` | 1 to 256. |
| `requirements` | At most 10, each exactly `name==version` with a plain version such as `2.32.3`. No URLs, VCS references, paths, extras, markers or options, and no name twice. They are installed with `--no-deps`, so list the whole dependency closure. |
| `examples` | 1 to 5. Every `input` must satisfy `input_schema`. `files` keys are relative paths that start with `inputs/`. `output`, when given, must satisfy `output_schema` if there is one. `live` defaults to `false`. |

Fields may be left out when the table says optional or the default is shown. Omitting a default and writing it explicitly
give the same package and the same hash.

## Tests

Put at least three top-level `def test_*` functions in files named `tests/test_*.py`; they are counted by reading the syntax tree.
Write them to assert on **values the tool returns**, because a later gate runs your tests against a stand-in that returns nothing and
rejects the package if they still pass (see "Not built yet"). Plain `assert` statements work best.

## Validating

```
toolforge validate <dir> [--json]     # exits 1 when there is any error; never runs package code
toolforge new <name> [--dir .]        # writes a valid starter package and prints the next command
```

`validate` prints each problem with where it is, what is wrong and what to change, then a one-line verdict. `--json` prints the
same report as `{"ok": ..., "issues": [...], "warnings": [...]}`; each entry has `path`, `code`, `message` and `hint`.
Errors reject a package; warnings are advice, and they appear once the manifest itself is valid (advice about a manifest that
cannot be read would only distract). Every problem is reported at once (up to 100 of each kind).

Reference packages that validate with no errors: `examples/toolpacks/word_count` and `examples/toolpacks/csv_summary`.

## Every issue code

The `code` of an issue is stable and safe to match on. This table is generated from the code that defines the rules
(`ISSUE_CODES` in `toolforge.models.package`); a test fails when it differs.

<!-- issue-codes:begin -->
| Code | Severity | Rule |
| --- | --- | --- |
| `manifest.missing` | error | The package has a manifest (manifest.json on disk, the `manifest` field in a submission). |
| `manifest.invalid_json` | error | manifest.json is valid JSON. |
| `manifest.not_object` | error | The manifest is a JSON object. |
| `manifest.too_deep` | error | The manifest nests at most 40 levels deep. |
| `manifest.not_json` | error | The manifest holds only JSON values: finite numbers and text that is valid Unicode. |
| `field.required` | error | Every required field is present. |
| `field.unknown` | error | Only the documented fields are used. |
| `field.type` | error | Every field has the documented JSON type. |
| `name.invalid` | error | `name` matches `[a-z][a-z0-9_]{2,40}`. |
| `name.reserved_prefix` | error | `name` does not start with `forge_` or `tf_`. |
| `version.invalid` | error | `version` is `MAJOR.MINOR.PATCH`: three whole numbers, no leading zeros, at most six digits each. |
| `parent_version.invalid` | error | `parent_version`, when set, has the same form as `version`. |
| `parent_version.not_lower` | error | `parent_version` is lower than `version`. |
| `description.length` | error | `description` is 20 to 600 characters after stripping. |
| `description.invalid_chars` | error | `description` is plain text: no control, invisible or line-separator characters. |
| `when_to_use.length` | error | `when_to_use` is at most 300 characters after stripping. |
| `when_to_use.invalid_chars` | error | `when_to_use` is plain text: no control, invisible or line-separator characters. |
| `entrypoint.format` | error | `entrypoint` is `<relative .py path>:<function name>`. |
| `entrypoint.missing_file` | error | The entrypoint file is in the package. |
| `entrypoint.missing_function` | error | The entrypoint file defines the function at module top level with plain `def` (not async, not an assignment). |
| `kind.invalid` | error | `kind` is one of: stateless, session. |
| `schema.invalid` | error | `input_schema` and `output_schema` are valid JSON Schema (Draft 2020-12) and every local `$ref` resolves. |
| `schema.not_object` | error | `input_schema` has top-level `type: object`. |
| `schema.additional_properties` | error | `input_schema` sets top-level `additionalProperties: false`. |
| `schema.remote_ref` | error | Schemas have no remote `$ref`: every reference starts with `#`. |
| `schema.too_deep` | error | Schemas nest subschemas at most 6 levels deep. |
| `schema.too_many_properties` | error | Schemas declare at most 40 properties in total. |
| `schema.too_expensive` | error | Checking the examples against the schemas finishes within 5 seconds. |
| `permissions.network.too_many` | error | `permissions.network` lists at most 8 hosts. |
| `permissions.network.invalid_host` | error | Each network host is a bare lower-case hostname: no scheme, port, path, wildcard or IP address. |
| `permissions.secrets.too_many` | error | `permissions.secrets` lists at most 4 names. |
| `permissions.secrets.invalid_name` | error | Each secret name matches `[a-z][a-z0-9_]{0,23}`. |
| `permissions.browser.reserved` | error | `permissions.browser` is false; it is reserved for a later release. |
| `limits.timeout_s.range` | error | `limits.timeout_s` is 1 to 120. |
| `limits.memory_mb.range` | error | `limits.memory_mb` is 64 to 512. |
| `limits.output_kb.range` | error | `limits.output_kb` is 1 to 256. |
| `requirements.too_many` | error | `requirements` lists at most 10 packages. |
| `requirements.invalid` | error | Each requirement is exactly `name==version` (no URLs, VCS references, paths, extras, markers or options) and no name appears twice. |
| `examples.count` | error | `examples` has 1 to 5 entries. |
| `examples.input_invalid` | error | Every example `input` is valid against `input_schema`. |
| `examples.file_invalid` | error | Example `files` are text, at most the per-file size limit, under a safe relative path that starts with `inputs/`. |
| `examples.output_invalid` | error | Every example `output`, when given, is valid against `output_schema` (when that exists). |
| `files.too_many` | error | The package holds at most 40 files. |
| `file.path` | error | File paths are plain relative paths: no `..`, no leading `/`, no backslash, no segment starting with a dot, no control characters, no symbolic links. |
| `file.extension` | error | File extensions are one of: .py, .json, .txt, .md, .csv. |
| `file.too_large` | error | Each file is at most 64 KB. |
| `file.encoding` | error | Files are valid UTF-8 text. |
| `file.nul` | error | Files contain no NUL characters. |
| `file.type` | error | In a submission every file's content is text. |
| `file.syntax` | error | Every `.py` file parses as Python. |
| `package.too_large` | error | The package, manifest included, is at most `max_package_kb` (setting `TOOLFORGE_MAX_PACKAGE_KB`). |
| `tests.too_few` | error | `tests/test_*.py` hold at least 3 top-level `def test_*` functions in total. |
| `package.internal_error` | error | Validation itself completes (if not, this is a ToolForge bug and the package is rejected). |
| `when_to_use.empty` | warning | `when_to_use` is filled in. |
| `output_schema.missing` | warning | `output_schema` is given. |
| `examples.no_output` | warning | At least one example gives an expected `output`. |
<!-- issue-codes:end -->

## How a tool runs

`toolforge run` and every later caller (the MCP server, the verification gates) run a tool the same way: in a fresh sandbox, one call
per sandbox, with no network. Your tool never sees anything from another call.

### What a tool looks like

```python
# tool.py
from toolforge_sdk import ToolError

def run(args: dict) -> object:
    """Return any JSON value. Raise ToolError(message) for a failure the caller can fix."""
```

`run` receives the caller's arguments, already checked against `input_schema`, and returns a JSON value: dicts, lists, text,
numbers, booleans or `None`. NaN, infinity, text that is not valid Unicode, a set or an object that is not JSON is `NotJson`; a result
larger than `limits.output_kb` is `OutputTooLarge`. `ToolError` is a handled failure that does not count against the tool; any other
exception, including `SystemExit`, is a `Crash` and does. Anything printed is captured, not returned: only the last 2000 characters of
stdout and of stderr reach the caller.

### The `toolforge_sdk` helpers

`toolforge_sdk` is written next to the harness and needs no installation; it is standard library only.

| Name | What it does |
| --- | --- |
| `ToolError(message)` | A handled failure. The message goes to the caller, so say what to change. |
| `input_path(name)` | The path of a file the caller provided, under `/workspace/inputs/`. `name` is relative to that folder, and one leading `inputs/` is accepted, so the `path` argument a caller sent (`inputs/data.csv`) works as given. Raises `ToolError` listing the files that were provided when there is no such file. |
| `output_path(name)` | `/workspace/outputs/<name>`, with its folders created. Files written there are discarded with the sandbox: delivering them to the caller is patch P14. Return results through `run`. |

### What is in the sandbox

```
/workspace/
  pkg/            your package (tool.py, tests/, and .deps/ when it has requirements)
  inputs/         the caller's files, only when permissions.inputs is true: inputs/<name>
  .tf/            harness.py, in.json, toolforge_sdk.py (written by the host; not yours)
```

The harness starts with `/workspace` as its working directory, so a relative path such as `inputs/data.csv` opens
`/workspace/inputs/data.csv` and `open(args["path"])` works. `/workspace/pkg` and `/workspace/pkg/.deps` are first on `sys.path`, but a
package cannot replace the standard library or `toolforge_sdk`: both are loaded before your folders are added. `PYTHONHASHSEED` is `0`
and bytecode is not written, so a tool that returns `list(some_set)` answers the same way every time.

Limits: the wall-clock timeout is `limits.timeout_s` (the setting default when unset) plus a 3 second grace; memory is
`limits.memory_mb`, enforced by the container; one file may not exceed 32 MB; a call may attach at most 20 files and 1 MB in total.
The caller never sees host paths or Docker detail.

### What comes back

The harness prints one line, `<<TF:<nonce>>>{json}<<TF-END:<nonce>>>`, on the real stdout; the nonce is random per call, `<` and `>` are
escaped inside the JSON, and the host reads only the last envelope that carries its nonce, with a strict shape, a second size check,
and every text field stripped of control characters and clipped. A tool can forge an envelope, but that only lets it report a
different result for itself, which it could return honestly. The host measures `duration_ms` itself and decides timeouts and kills
by what it observed, never by what the envelope says.

| `error.type` | Decided by | Meaning |
| --- | --- | --- |
| `InvalidArguments`, `InvalidFiles` | host, before any sandbox | The caller's arguments or files are wrong. Nothing ran; the tool is not blamed. |
| `ToolError` | harness | The tool refused on purpose. Not counted against the tool. |
| `Crash` | harness | The tool raised, or the entrypoint could not be loaded. Counted against the tool. |
| `NotJson`, `OutputTooLarge` | harness (checked again by host) | The result cannot be returned. |
| `Timeout` | host | The tool did not finish in time and the sandbox was stopped. |
| `SandboxCrashed` | host | The sandbox or the process was killed from outside (memory is the usual cause). |
| `NoResult` | host | The process ended without an envelope (for example `os._exit`). |

### Running one yourself

```
toolforge run <name or name@version> --args '{...}' [--file path ...]        # an active, registered version
toolforge run --package <dir> --args '{...}' --dev-unverified [--file ...]    # a folder, with no gate having run
```

Each `--file` appears as `inputs/<file name>`. The result prints as JSON (ASCII only) and the exit code is 0 when the call succeeded.
`--dev-unverified` still runs only inside a sandbox, prints a warning, and records nothing in the registry. Both need Docker.

## What the package hash means

`code_sha` is the SHA-256 of a deterministic archive of the package: files sorted by name, modification time 0, owner 0, mode
0644, and the manifest rewritten in a canonical form (sorted keys, two-space indent). The same content always gives the same
hash, whatever order it arrived in or which machine built it. Line endings count: a file with CRLF line endings is a different
file from the same text with LF.

## How validation treats your package

A package is untrusted input, so validation is static. It never runs or imports your code: Python files are only parsed. Text from
your package that appears in a message is shortened and stripped of unprintable characters. Checking examples against a schema
runs in a separate process with a time and memory limit, because a hostile schema (a catastrophic regular expression, or references
that fan out) can otherwise keep the checker busy for hours; if it cannot finish in 5 seconds the package is rejected with
`schema.too_expensive`.

### Limits of this check

* It proves a package is well formed, not that it is safe or correct. Safety comes from running tools in a sandbox, and correctness
  from the gates that run them.
* A `$ref` that uses a plain-name anchor (`#name`) is accepted without checking that the anchor exists. JSON-pointer references
  (`#/$defs/x`) are checked.
* Schema keywords are checked for validity, but `format` is an annotation and is not enforced.
* The entrypoint check looks for a top-level `def`. It cannot know whether the function accepts one argument or returns JSON; running
  the tool finds that out.

## Not built yet

These parts of the format are described in the guide but are not implemented, so nothing above depends on them:

* Running your tests, the stand-in test-strength check, building `requirements`, and schema fuzzing: patch P06.
* Sessions (`kind: session`): patch P13.
* The browser permission, and delivering the files a tool writes with `output_path`: patch P14.
