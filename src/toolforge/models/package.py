"""The tool package: the manifest model, the catalogue of validation issues, and `ToolPackage`.

A package is a manifest plus source files (`docs/guide/package-format.md`). Everything later (registry, pipeline, runner, MCP
server, builder) speaks this type.

Validation is static and never raises on bad content. A package comes from a tool author, so it is hostile input:

* No package code is executed or imported here, ever. Python files are read with `ast` only
  (`tests/test_cli_package.py::test_validate_never_runs_package_code`).
* Anything that could crash or stall validation (deep nesting, huge numbers, lone surrogates, regex or reference bombs in a
  schema, hundreds of files) becomes an issue instead (`tests/test_package_manifest.py`, `tests/test_package_isolation.py`).
* Text from the package that is echoed in a message is clipped and escaped (`_clip`, `_safe`).

The split of work: `Manifest` enforces types and the per-field value rules, each failure carrying a stable issue code.
`ToolPackage.validate` adds the rules that need the files or the schemas (entrypoint, tests, file limits, examples against
schemas). Every issue code is listed once, in `ISSUE_CODES`; `docs/guide/package-format.md` shows that table and a drift test
(`tests/test_package_docs.py`) keeps the two equal.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import keyword
import math
import re
import reprlib
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import PydanticCustomError

from toolforge.errors import ValidationError as PackageInvalid
from toolforge.models.results import Issue, ValidationReport
from toolforge.packaging import (
    ALLOWED_EXTENSIONS,
    MANIFEST_NAMES,
    MAX_FILE_BYTES,
    MAX_FILES,
    Problem,
    build_tar,
    canonical_manifest_bytes,
    check_rel_path,
    read_package_dir,
    text_problem,
    write_package_dir,
)
from toolforge.schemacheck import SchemaCheckError, check_instances

if TYPE_CHECKING:
    from toolforge.config import Settings

# --- Limits (each appears once here; the issue catalogue and the docs are built from these) -----------------------------

NAME_PATTERN = r"[a-z][a-z0-9_]{2,40}"
RESERVED_NAME_PREFIXES = ("forge_", "tf_")
DESCRIPTION_MIN, DESCRIPTION_MAX, WHEN_TO_USE_MAX = 20, 600, 300
MAX_HOSTS, MAX_SECRETS, MAX_REQUIREMENTS = 8, 4, 10
SECRET_PATTERN = r"[a-z][a-z0-9_]{0,23}"
MIN_EXAMPLES, MAX_EXAMPLES = 1, 5
MIN_TESTS = 3
TIMEOUT_RANGE, MEMORY_RANGE, OUTPUT_RANGE = (1, 120), (64, 512), (1, 256)
MAX_SCHEMA_DEPTH, MAX_SCHEMA_PROPERTIES = 6, 40
MAX_JSON_DEPTH = 40
MAX_INT_BITS = 1024
MAX_ISSUES = 100
SCHEMA_CHECK_TIMEOUT_S = 5.0
SCHEMA_CHECK_ISOLATED = (
    True  # tests of other rules turn this off for speed; the shipped behaviour is isolated
)
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
KINDS = ("stateless", "session")
TEST_FILE = re.compile(r"tests/test_[^/]+\.py")


# --- The issue catalogue -------------------------------------------------------------------------------------------------


class CodeInfo(NamedTuple):
    severity: str  # "error" rejects the package; "warning" is advice
    rule: str  # the rule, in one line, as shown in the docs
    hint: str  # what to change, as a short imperative sentence


_EXT = ", ".join(ALLOWED_EXTENSIONS)
_FIELDS_HINT = "Fix the field; docs/guide/package-format.md lists every field and rule."

ISSUE_CODES: dict[str, CodeInfo] = {
    # The manifest as a whole
    "manifest.missing": CodeInfo(
        "error",
        "The package has a manifest (manifest.json on disk, the `manifest` field in a submission).",
        "Add manifest.json at the package root; `toolforge new <name>` writes a valid one.",
    ),
    "manifest.invalid_json": CodeInfo(
        "error", "manifest.json is valid JSON.", "Fix the JSON syntax at the reported line and column."
    ),
    "manifest.not_object": CodeInfo(
        "error", "The manifest is a JSON object.", "Make the manifest an object with the documented fields."
    ),
    "manifest.too_deep": CodeInfo(
        "error",
        f"The manifest nests at most {MAX_JSON_DEPTH} levels deep.",
        "Flatten the manifest; schemas and example values should stay small.",
    ),
    "manifest.not_json": CodeInfo(
        "error",
        "The manifest holds only JSON values: finite numbers and text that is valid Unicode.",
        "Remove NaN, Infinity, huge integers and unpaired surrogates from the manifest.",
    ),
    "field.required": CodeInfo("error", "Every required field is present.", "Add the missing field."),
    "field.unknown": CodeInfo(
        "error", "Only the documented fields are used.", "Remove the field or fix its spelling."
    ),
    "field.type": CodeInfo(
        "error", "Every field has the documented JSON type.", "Use the documented type for the field."
    ),
    # Identity and text
    "name.invalid": CodeInfo(
        "error",
        f"`name` matches `{NAME_PATTERN}`.",
        "Use 3 to 41 characters: a lower-case letter first, then lower-case letters, digits or underscores.",
    ),
    "name.reserved_prefix": CodeInfo(
        "error",
        "`name` does not start with `forge_` or `tf_`.",
        "Pick a name without the forge_ or tf_ prefix; those belong to ToolForge.",
    ),
    "version.invalid": CodeInfo(
        "error",
        "`version` is `MAJOR.MINOR.PATCH`: three whole numbers, no leading zeros, at most six digits each.",
        "Write the version as MAJOR.MINOR.PATCH, for example 1.0.0.",
    ),
    "parent_version.invalid": CodeInfo(
        "error",
        "`parent_version`, when set, has the same form as `version`.",
        "Write parent_version as MAJOR.MINOR.PATCH, or use null.",
    ),
    "parent_version.not_lower": CodeInfo(
        "error",
        "`parent_version` is lower than `version`.",
        "Set parent_version to the version this one replaces, or null for a first version.",
    ),
    "description.length": CodeInfo(
        "error",
        f"`description` is {DESCRIPTION_MIN} to {DESCRIPTION_MAX} characters after stripping.",
        "Write one or two plain sentences saying what the tool does.",
    ),
    "description.invalid_chars": CodeInfo(
        "error",
        "`description` is plain text: no control, invisible or line-separator characters.",
        "Use a single line of plain text.",
    ),
    "when_to_use.length": CodeInfo(
        "error",
        f"`when_to_use` is at most {WHEN_TO_USE_MAX} characters after stripping.",
        "Shorten it to the situation in which an agent should pick this tool.",
    ),
    "when_to_use.invalid_chars": CodeInfo(
        "error",
        "`when_to_use` is plain text: no control, invisible or line-separator characters.",
        "Use a single line of plain text.",
    ),
    # Entrypoint and kind
    "entrypoint.format": CodeInfo(
        "error",
        "`entrypoint` is `<relative .py path>:<function name>`.",
        "Write it as tool.py:run: a relative .py path, a colon, then the function name.",
    ),
    "entrypoint.missing_file": CodeInfo(
        "error",
        "The entrypoint file is in the package.",
        "Add the file to the package or fix the path in entrypoint.",
    ),
    "entrypoint.missing_function": CodeInfo(
        "error",
        "The entrypoint file defines the function at module top level with plain `def` (not async, not an assignment).",
        "Define it as `def name(args):` at the top level of the file.",
    ),
    "kind.invalid": CodeInfo(
        "error", f"`kind` is one of: {', '.join(KINDS)}.", f"Use one of: {', '.join(KINDS)}."
    ),
    # Schemas
    "schema.invalid": CodeInfo(
        "error",
        "`input_schema` and `output_schema` are valid JSON Schema (Draft 2020-12) and every local `$ref` resolves.",
        "Fix the schema; the message names the failing keyword.",
    ),
    "schema.not_object": CodeInfo(
        "error",
        "`input_schema` has top-level `type: object`.",
        'Set "type": "object" at the top of input_schema.',
    ),
    "schema.additional_properties": CodeInfo(
        "error",
        "`input_schema` sets top-level `additionalProperties: false`.",
        'Add "additionalProperties": false at the top of input_schema.',
    ),
    "schema.remote_ref": CodeInfo(
        "error",
        "Schemas have no remote `$ref`: every reference starts with `#`.",
        "Inline the definition or use a local reference such as #/$defs/name.",
    ),
    "schema.too_deep": CodeInfo(
        "error",
        f"Schemas nest subschemas at most {MAX_SCHEMA_DEPTH} levels deep.",
        "Flatten the schema or split the tool in two.",
    ),
    "schema.too_many_properties": CodeInfo(
        "error",
        f"Schemas declare at most {MAX_SCHEMA_PROPERTIES} properties in total.",
        "Accept fewer properties, or group some of them.",
    ),
    "schema.too_expensive": CodeInfo(
        "error",
        f"Checking the examples against the schemas finishes within {SCHEMA_CHECK_TIMEOUT_S:g} seconds.",
        "Simplify the schemas: avoid long reference chains and backtracking-heavy patterns.",
    ),
    # Permissions and limits
    "permissions.network.too_many": CodeInfo(
        "error",
        f"`permissions.network` lists at most {MAX_HOSTS} hosts.",
        "Declare only the hosts the tool really contacts.",
    ),
    "permissions.network.invalid_host": CodeInfo(
        "error",
        "Each network host is a bare lower-case hostname: no scheme, port, path, wildcard or IP address.",
        "Write hosts like api.example.com: lower case, without https://, a port or *.",
    ),
    "permissions.secrets.too_many": CodeInfo(
        "error",
        f"`permissions.secrets` lists at most {MAX_SECRETS} names.",
        "Declare only the secrets the tool really needs.",
    ),
    "permissions.secrets.invalid_name": CodeInfo(
        "error",
        f"Each secret name matches `{SECRET_PATTERN}`.",
        "Use a lower-case name such as weather_key: letters, digits and underscores.",
    ),
    "permissions.browser.reserved": CodeInfo(
        "error",
        "`permissions.browser` is false; it is reserved for a later release.",
        "Set browser to false.",
    ),
    "limits.timeout_s.range": CodeInfo(
        "error",
        f"`limits.timeout_s` is {TIMEOUT_RANGE[0]} to {TIMEOUT_RANGE[1]}.",
        "Use a value in range, or leave it out for the default.",
    ),
    "limits.memory_mb.range": CodeInfo(
        "error",
        f"`limits.memory_mb` is {MEMORY_RANGE[0]} to {MEMORY_RANGE[1]}.",
        "Use a value in range, or leave it out for the default.",
    ),
    "limits.output_kb.range": CodeInfo(
        "error",
        f"`limits.output_kb` is {OUTPUT_RANGE[0]} to {OUTPUT_RANGE[1]}.",
        "Use a value in range, or leave it out for the default.",
    ),
    # Requirements and examples
    "requirements.too_many": CodeInfo(
        "error",
        f"`requirements` lists at most {MAX_REQUIREMENTS} packages.",
        "List fewer packages; prefer the standard library.",
    ),
    "requirements.invalid": CodeInfo(
        "error",
        "Each requirement is exactly `name==version` (no URLs, VCS references, paths, extras, markers or options) "
        "and no name appears twice.",
        "Write each as name==version, for example requests==2.32.3, and list the whole dependency closure.",
    ),
    "examples.count": CodeInfo(
        "error",
        f"`examples` has {MIN_EXAMPLES} to {MAX_EXAMPLES} entries.",
        f"Provide between {MIN_EXAMPLES} and {MAX_EXAMPLES} examples.",
    ),
    "examples.input_invalid": CodeInfo(
        "error",
        "Every example `input` is valid against `input_schema`.",
        "Change the example input to satisfy input_schema, or fix the schema.",
    ),
    "examples.file_invalid": CodeInfo(
        "error",
        "Example `files` are text, at most the per-file size limit, under a safe relative path that starts with `inputs/`.",
        "Name example files inputs/<file> and keep them small text.",
    ),
    "examples.output_invalid": CodeInfo(
        "error",
        "Every example `output`, when given, is valid against `output_schema` (when that exists).",
        "Change the example output to satisfy output_schema, or fix the schema.",
    ),
    # Files
    "files.too_many": CodeInfo(
        "error",
        f"The package holds at most {MAX_FILES} files.",
        f"Remove files; at most {MAX_FILES} are allowed.",
    ),
    "file.path": CodeInfo(
        "error",
        "File paths are plain relative paths: no `..`, no leading `/`, no backslash, no segment starting with a dot, "
        "no control characters, no symbolic links.",
        "Use relative paths with forward slashes and no hidden folders.",
    ),
    "file.extension": CodeInfo(
        "error",
        f"File extensions are one of: {_EXT}.",
        f"Rename or remove the file; allowed extensions are {_EXT}.",
    ),
    "file.too_large": CodeInfo(
        "error",
        f"Each file is at most {MAX_FILE_BYTES // 1024} KB.",
        f"Keep each file under {MAX_FILE_BYTES // 1024} KB; keep bulk data out of the package.",
    ),
    "file.encoding": CodeInfo("error", "Files are valid UTF-8 text.", "Save the file as UTF-8 text."),
    "file.nul": CodeInfo(
        "error", "Files contain no NUL characters.", "Remove NUL characters; packages are text only."
    ),
    "file.type": CodeInfo(
        "error", "In a submission every file's content is text.", "Send every file as text: {path: contents}."
    ),
    "file.syntax": CodeInfo(
        "error", "Every `.py` file parses as Python.", "Fix the syntax error; the message gives the line."
    ),
    "package.too_large": CodeInfo(
        "error",
        "The package, manifest included, is at most `max_package_kb` (setting `TOOLFORGE_MAX_PACKAGE_KB`).",
        "Remove files or shrink the examples.",
    ),
    "tests.too_few": CodeInfo(
        "error",
        f"`tests/test_*.py` hold at least {MIN_TESTS} top-level `def test_*` functions in total.",
        f"Write at least {MIN_TESTS} tests that assert on the values the tool returns.",
    ),
    "package.internal_error": CodeInfo(
        "error",
        "Validation itself completes (if not, this is a ToolForge bug and the package is rejected).",
        "Retry once; if it persists, report it as a ToolForge bug.",
    ),
    # Warnings
    "when_to_use.empty": CodeInfo(
        "warning",
        "`when_to_use` is filled in.",
        "Add a short when_to_use so agents know when to pick this tool.",
    ),
    "output_schema.missing": CodeInfo(
        "warning", "`output_schema` is given.", "Add output_schema so callers know what the tool returns."
    ),
    "examples.no_output": CodeInfo(
        "warning",
        "At least one example gives an expected `output`.",
        "Give at least one example an expected output so it checks behaviour.",
    ),
}


def issue_codes_markdown() -> str:
    """The catalogue as a Markdown table. `docs/guide/package-format.md` embeds exactly this text (drift-tested)."""
    rows = ["| Code | Severity | Rule |", "| --- | --- | --- |"]
    for code, info in ISSUE_CODES.items():
        rows.append(f"| `{code}` | {info.severity} | {info.rule.replace('|', '/')} |")
    return "\n".join(rows)


# --- Small helpers -------------------------------------------------------------------------------------------------------

_REPR = reprlib.Repr()
_REPR.maxstring = _REPR.maxother = 60
_REPR.maxlevel, _REPR.maxlist, _REPR.maxdict, _REPR.maxtuple = 2, 4, 4, 4


def _clip(value: object) -> str:
    """A short, escaped rendering of untrusted text or data for use inside a message."""
    return _REPR.repr(value)


def _safe(text: str, limit: int = 100) -> str:
    return "".join(char if char.isprintable() else "?" for char in text)[:limit]


def _err(code: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(code, "{message}", {"message": message})


def _loc_path(loc: tuple[int | str, ...]) -> str:
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{_safe(part, 60)}" if path else _safe(part, 60))
    return path


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def json_problem(value: Any) -> tuple[str, str] | None:
    """`(code, message)` when `value` is not plain, bounded JSON data; None when it is. Iterative, so depth cannot crash it."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            return "manifest.too_deep", f"nests more than {MAX_JSON_DEPTH} levels deep"
        kind = type(node)
        if node is None or kind is bool:
            continue
        if kind is int:
            if node.bit_length() > MAX_INT_BITS:
                return "manifest.not_json", "contains an integer that is far too large"
        elif kind is float:
            if not math.isfinite(node):
                return "manifest.not_json", "contains NaN or Infinity, which JSON does not allow"
        elif kind is str:
            if not _encodable(node):
                return "manifest.not_json", "contains text that is not valid Unicode (an unpaired surrogate)"
        elif kind is list:
            stack.extend((child, depth + 1) for child in node)
        elif kind is dict:
            for key, child in node.items():
                if type(key) is not str or not _encodable(key):
                    return "manifest.not_json", "has an object key that is not valid text"
                stack.append((child, depth + 1))
        else:
            return "manifest.not_json", f"contains a {kind.__name__} value, which is not JSON"
    return None


# --- Field rules ---------------------------------------------------------------------------------------------------------


def name_problem(value: str) -> tuple[str, str] | None:
    """`(code, message)` when `value` is not an acceptable tool name. Also used by `toolforge new`."""
    if not re.fullmatch(NAME_PATTERN, value):
        return "name.invalid", f"name {_clip(value)} does not match {NAME_PATTERN}"
    if value.startswith(RESERVED_NAME_PREFIXES):
        return "name.reserved_prefix", f"name {_clip(value)} starts with a prefix reserved for ToolForge"
    return None


_SEMVER = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")


def semver_tuple(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER.fullmatch(value)
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def _invisible_problem(text: str) -> str | None:
    for index, char in enumerate(text):
        category = unicodedata.category(char)
        if category[0] == "C" or category in {"Zl", "Zp"}:
            return f"contains a control, invisible or line-separator character (U+{ord(char):04X}) at position {index}"
    return None


def _check_name(value: str) -> str:
    problem = name_problem(value)
    if problem:
        raise _err(*problem)
    return value


def _check_version(value: str) -> str:
    if semver_tuple(value) is None:
        raise _err("version.invalid", f"version {_clip(value)} is not MAJOR.MINOR.PATCH (for example 1.0.0)")
    return value


def _check_parent_version(value: str | None) -> str | None:
    if value is not None and semver_tuple(value) is None:
        raise _err("parent_version.invalid", f"parent_version {_clip(value)} is not MAJOR.MINOR.PATCH")
    return value


def _check_description(value: str) -> str:
    text = value.strip()
    problem = _invisible_problem(text)
    if problem:
        raise _err("description.invalid_chars", f"description {problem}")
    if not DESCRIPTION_MIN <= len(text) <= DESCRIPTION_MAX:
        raise _err(
            "description.length",
            f"description is {len(text)} characters after stripping; it must be {DESCRIPTION_MIN} to {DESCRIPTION_MAX}",
        )
    return text


def _check_when_to_use(value: str) -> str:
    text = value.strip()
    problem = _invisible_problem(text)
    if problem:
        raise _err("when_to_use.invalid_chars", f"when_to_use {problem}")
    if len(text) > WHEN_TO_USE_MAX:
        raise _err(
            "when_to_use.length",
            f"when_to_use is {len(text)} characters after stripping; the most allowed is {WHEN_TO_USE_MAX}",
        )
    return text


_FUNCTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def entrypoint_problem(value: str) -> str | None:
    """Why `value` is not `<path>.py:<function>`, or None when it is."""
    path, colon, function = value.partition(":")
    if not colon:
        return "has no colon; write it as tool.py:run"
    problem = check_rel_path(path)
    if problem:
        return f"the file path {problem}"
    if not path.endswith(".py"):
        return "the file must be a .py file"
    if not _FUNCTION_NAME.fullmatch(function) or keyword.iskeyword(function):
        return "the function name must be a plain Python identifier"
    return None


def _check_entrypoint(value: str | None) -> str | None:
    if value is None:
        return None
    problem = entrypoint_problem(value)
    if problem:
        raise _err("entrypoint.invalid", f"entrypoint {_clip(value)} {problem}")
    return value


def _check_kind(value: str) -> str:
    if value not in ("stateless", "session", "sql", "file-format", "http-api", "game-state", "browser-plan"):
        raise _err("kind.invalid", f"kind {repr(value)} is not one of: stateless, session, sql, file-format, http-api, game-state, browser-plan")
    return value


_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _host_problem(host: str) -> str | None:
    if "://" in host or "/" in host or "?" in host or "@" in host:
        return "must be a bare hostname, without a scheme, path or credentials"
    if ":" in host:
        return "must not carry a port (or be an IPv6 address)"
    if "*" in host:
        return "must not contain a wildcard"
    if host != host.lower():
        return "must be lower case"
    if not 1 <= len(host) <= 253 or any(not _HOST_LABEL.fullmatch(label) for label in host.split(".")):
        return "is not a valid hostname (letters, digits and hyphens in dot-separated labels)"
    if not host.rsplit(".", 1)[-1][0].isalpha():
        return "looks like an IP address or a number; name the host instead"
    return None


def _check_host(value: str) -> str:
    problem = _host_problem(value)
    if problem:
        raise _err("permissions.network.invalid_host", f"host {_clip(value)} {problem}")
    return value


def _check_secret(value: str) -> str:
    if not re.fullmatch(SECRET_PATTERN, value):
        raise _err(
            "permissions.secrets.invalid_name", f"secret name {_clip(value)} does not match {SECRET_PATTERN}"
        )
    return value


def _check_browser(value: bool) -> bool:
    if value:
        raise _err("permissions.browser.reserved", "permissions.browser must be false; it is reserved")
    return value


def _max_items(code: str, label: str, maximum: int) -> Callable[[list[Any]], list[Any]]:
    def check(value: list[Any]) -> list[Any]:
        if len(value) > maximum:
            raise _err(code, f"{label} has {len(value)} entries; the most allowed is {maximum}")
        return value

    return check


def _in_range(code: str, label: str, low: int, high: int) -> Callable[[int], int]:
    def check(value: int) -> int:
        if not low <= value <= high:
            raise _err(code, f"{label} is {value}; it must be between {low} and {high}")
        return value

    return check


_REQ_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_REQ_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?")
_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")


def _requirement_problem(text: str) -> str | None:
    if len(text) > 100:
        return "is longer than 100 characters"
    if text.startswith("-"):
        return "looks like a pip option; options are not allowed"
    if "://" in text or "@" in text or text.startswith(_VCS_PREFIXES):
        return "looks like a URL or VCS reference; only name==version is allowed"
    if "/" in text or "\\" in text or text.endswith((".whl", ".zip", ".gz")):
        return "looks like a path or an archive; only name==version is allowed"
    if "[" in text:
        return "uses extras; list the extra's own packages instead"
    if ";" in text:
        return "uses an environment marker; markers are not allowed"
    name, equals, version = text.partition("==")
    if not equals or not _REQ_NAME.fullmatch(name) or not _REQ_VERSION.fullmatch(version):
        return "must be exactly name==version with a plain version such as 1.2.3 (no spaces, ranges or local tags)"
    return None


def _check_requirement(value: str) -> str:
    problem = _requirement_problem(value)
    if problem:
        raise _err("requirements.invalid", f"requirement {_clip(value)} {problem}")
    return value


def _check_requirements(value: list[str]) -> list[str]:
    if len(value) > MAX_REQUIREMENTS:
        raise _err(
            "requirements.too_many",
            f"requirements has {len(value)} entries; the most allowed is {MAX_REQUIREMENTS}",
        )
    names = [re.sub(r"[-_.]+", "-", item.partition("==")[0]).lower() for item in value]
    for name in sorted(set(names)):
        if names.count(name) > 1:
            raise _err("requirements.invalid", f"requirements lists {_clip(name)} more than once")
    return value


def _check_examples(value: list[Any]) -> list[Any]:
    if not MIN_EXAMPLES <= len(value) <= MAX_EXAMPLES:
        raise _err(
            "examples.count",
            f"examples has {len(value)} entries; it must have {MIN_EXAMPLES} to {MAX_EXAMPLES}",
        )
    return value


Hostname = Annotated[str, AfterValidator(_check_host)]
SecretName = Annotated[str, AfterValidator(_check_secret)]
Requirement = Annotated[str, AfterValidator(_check_requirement)]
TimeoutSeconds = Annotated[
    int, AfterValidator(_in_range("limits.timeout_s.range", "timeout_s", *TIMEOUT_RANGE))
]
MemoryMegabytes = Annotated[
    int, AfterValidator(_in_range("limits.memory_mb.range", "memory_mb", *MEMORY_RANGE))
]
OutputKilobytes = Annotated[
    int, AfterValidator(_in_range("limits.output_kb.range", "output_kb", *OUTPUT_RANGE))
]


# --- The manifest model --------------------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Permissions(_Model):
    """What the tool may touch. Least privilege: everything is off unless declared."""

    network: Annotated[
        list[Hostname], AfterValidator(_max_items("permissions.network.too_many", "network", MAX_HOSTS))
    ] = Field(default_factory=list)
    secrets: Annotated[
        list[SecretName], AfterValidator(_max_items("permissions.secrets.too_many", "secrets", MAX_SECRETS))
    ] = Field(default_factory=list)
    inputs: bool = False
    browser: Annotated[bool, AfterValidator(_check_browser)] = False


class Limits(_Model):
    """Resource limits. `None` means "not set": the settings defaults apply at run time."""

    timeout_s: TimeoutSeconds | None = None
    memory_mb: MemoryMegabytes | None = None
    output_kb: OutputKilobytes | None = None


class Example(_Model):
    """One worked example. `output: null` means "no expected output given"."""

    input: dict[str, Any]
    files: dict[str, str] = Field(default_factory=dict)
    output: Any = None
    live: bool = False


class Manifest(_Model):
    """The manifest. Types and per-field rules are enforced here; see `ToolPackage.validate` for the rest."""

    name: Annotated[str, AfterValidator(_check_name)]
    version: Annotated[str, AfterValidator(_check_version)]
    parent_version: Annotated[str | None, AfterValidator(_check_parent_version)] = None
    description: Annotated[str, AfterValidator(_check_description)]
    when_to_use: Annotated[str, AfterValidator(_check_when_to_use)] = ""
    entrypoint: Annotated[str | None, AfterValidator(_check_entrypoint)] = None
    kind: Annotated[str, AfterValidator(_check_kind)] = "stateless"
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    permissions: Permissions = Field(default_factory=Permissions)
    limits: Limits = Field(default_factory=Limits)
    requirements: Annotated[list[Requirement], AfterValidator(_check_requirements)] = Field(
        default_factory=list
    )
    examples: Annotated[list[Example], AfterValidator(_check_examples)]


# --- Schema rules --------------------------------------------------------------------------------------------------------

_SCHEMA_CHILD = ("items", "additionalProperties", "additionalItems", "contains", "propertyNames", "not")
_SCHEMA_CHILD += ("if", "then", "else", "unevaluatedItems", "unevaluatedProperties", "contentSchema")
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")


class _SchemaScan:
    def __init__(self) -> None:
        self.max_depth = 0
        self.properties = 0
        self.remote: list[tuple[str, str]] = []
        self.local: list[tuple[str, str]] = []


def _scan_schema(node: Any, path: str, depth: int, scan: _SchemaScan) -> None:
    """Visit every subschema once. Stops descending past the depth limit, so recursion here is bounded."""
    if not isinstance(node, dict):
        return
    scan.max_depth = max(scan.max_depth, depth)
    if depth > MAX_SCHEMA_DEPTH:
        return
    for keyword_ in ("$ref", "$dynamicRef"):
        ref = node.get(keyword_)
        if isinstance(ref, str):
            if not ref.startswith("#"):
                scan.remote.append((f"{path}.{keyword_}", ref))
            elif keyword_ == "$ref":
                scan.local.append((f"{path}.$ref", ref))
    for key in _SCHEMA_CHILD:
        _scan_schema(node.get(key), f"{path}.{key}", depth + 1, scan)
    for key in _SCHEMA_LISTS:
        children = node.get(key)
        if isinstance(children, list):
            for index, child in enumerate(children):
                _scan_schema(child, f"{path}.{key}[{index}]", depth + 1, scan)
    for key in _SCHEMA_MAPS:
        children = node.get(key)
        if isinstance(children, dict):
            if key == "properties":
                scan.properties += len(children)
            for name, child in children.items():
                _scan_schema(child, f"{path}.{key}.{_safe(str(name), 40)}", depth + 1, scan)


def _ref_resolves(root: Any, ref: str) -> bool:
    """Does a `#/json/pointer` reference point at something in the schema? Anchors (`#name`) are not checked."""
    if not ref.startswith("#/"):
        return True
    node = root
    for part in ref[2:].split("/"):
        part = urllib.parse.unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and len(part) <= 6 and int(part) < len(node):
            node = node[int(part)]
        else:
            return False
    return True


def _jsonschema_problem(schema: dict[str, Any]) -> str | None:
    declared = schema.get("$schema")
    if declared is not None and declared != DRAFT_2020_12:
        return f"$schema is {_clip(declared)}; only {DRAFT_2020_12} (or no $schema) is supported"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        where = "/".join(str(part) for part in exc.absolute_path)
        return _safe(exc.message, 200) + (f" (at /{where})" if where else "")
    except Exception as exc:
        return f"could not be checked ({type(exc).__name__})"
    return None


# --- The package ---------------------------------------------------------------------------------------------------------


class _Collector:
    """Gathers issues, capped, with each issue's hint taken from the catalogue."""

    def __init__(self) -> None:
        self.errors: list[Issue] = []
        self.warnings: list[Issue] = []

    def add(self, code: str, path: str, message: str, hint: str | None = None) -> None:
        info = ISSUE_CODES[
            code
        ]  # an unknown code is a bug; the caller of `validate` turns it into package.internal_error
        target = self.errors if info.severity == "error" else self.warnings
        if len(target) < MAX_ISSUES:
            target.append(
                Issue(path=path, code=code, message=message, hint=info.hint if hint is None else hint)
            )

    def report(self) -> ValidationReport:
        return ValidationReport(issues=self.errors, warnings=self.warnings)


def _parse_python(text: str) -> ast.Module | str:
    """The syntax tree, or a one-line reason there is none. Parses only; the source is never compiled or run."""
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        return f"{_safe(exc.msg, 80)} (line {exc.lineno})"
    except (ValueError, RecursionError, MemoryError) as exc:
        return f"could not be parsed ({type(exc).__name__})"


def _top_level_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


class ToolPackage:
    """A manifest plus source files. Treat it as immutable once built.

    `files` maps relative path to text and does not include `manifest.json` (that is `manifest`). Build one with
    `from_dir` or `from_submission`; both accept hostile content and report problems through `validate`.
    """

    def __init__(self, manifest: Any, files: Mapping[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files) if files else {}
        self._problems: list[Problem] = []
        self._manifest_explained = False
        self._skipped: set[str] = set()
        self._parsed: Manifest | None = None
        self._raw: Any = None
        if isinstance(manifest, Manifest):
            self._parsed = manifest
            self._raw = manifest.model_dump(mode="json")
        elif manifest is not None:
            problem = json_problem(manifest)
            if problem:
                self._problems.append(("manifest", *problem))
                self._manifest_explained = True
            else:
                self._raw = copy.deepcopy(manifest)

    # -- construction -------------------------------------------------------------------------------------------------

    @classmethod
    def from_dir(cls, path: str | Path) -> ToolPackage:
        """Read a package directory. Nothing in it is executed. Only a missing directory raises."""
        loaded = read_package_dir(path)
        package = cls(loaded.manifest, loaded.files)
        package._problems = [*loaded.problems, *package._problems]
        package._skipped = set(loaded.skipped)
        package._manifest_explained = package._manifest_explained or loaded.manifest_explained
        return package

    @classmethod
    def from_submission(cls, payload: Any) -> ToolPackage:
        """Build from `{"manifest": {...}, "files": {...}}`. Anything else becomes an issue reported by `validate`."""
        if not isinstance(payload, dict):
            package = cls(None, {})
            package._problems.append(
                (
                    "submission",
                    "field.type",
                    f"expected an object with manifest and files, got {type(payload).__name__}",
                )
            )
            package._manifest_explained = True
            return package
        problems: list[Problem] = []
        for key in payload:
            if key not in {"manifest", "files"}:
                problems.append(
                    (_safe(str(key), 60), "field.unknown", "a submission has only manifest and files")
                )
        files = payload.get("files")
        if "files" not in payload:
            problems.append(("files", "field.required", "the submission has no files"))
            files = {}
        elif not isinstance(files, dict):
            problems.append(
                (
                    "files",
                    "field.type",
                    f"files must be an object of path to text, got {type(files).__name__}",
                )
            )
            files = {}
        package = cls(payload.get("manifest"), files)
        package._problems = [*problems, *package._problems]
        return package

    # -- the manifest, parsed ------------------------------------------------------------------------------------------

    @property
    def manifest(self) -> Manifest:
        """The parsed manifest. Meaningful once `validate().ok`; raises `ValidationError` when the manifest is invalid."""
        if self._parsed is None:
            try:
                self._parsed = Manifest.model_validate(self._raw)
            except PydanticValidationError:
                raise PackageInvalid(
                    "the manifest is not valid, so it cannot be used",
                    code="manifest_invalid",
                    hint="call validate() and fix the reported issues first",
                ) from None
        return self._parsed

    # -- archive and hash ----------------------------------------------------------------------------------------------

    def to_tar(self) -> bytes:
        """The deterministic archive: `manifest.json` (canonical) and every file, sorted, mtime 0, uid and gid 0, mode 0644."""
        manifest = self.manifest
        if len(self.files) > MAX_FILES:
            raise PackageInvalid(f"the package has more than {MAX_FILES} files", code="package_invalid")
        entries = {MANIFEST_NAMES[0]: canonical_manifest_bytes(manifest.model_dump(mode="json"))}
        for path, text in self.files.items():
            if (
                check_rel_path(path)
                or path in MANIFEST_NAMES
                or not isinstance(text, str)
                or text_problem(text)
            ):
                raise PackageInvalid(
                    f"file {_clip(path)} cannot be archived",
                    code="package_invalid",
                    hint="call validate() and fix the reported issues first",
                )
            entries[path] = text.encode("utf-8")
        return build_tar(entries)

    @property
    def code_sha(self) -> str:
        """SHA-256 of the deterministic archive: the identity of this exact content."""
        return hashlib.sha256(self.to_tar()).hexdigest()

    def to_dir(self, dest: str | Path) -> Path:
        """Write the package to a new or empty directory (used by `toolforge new`)."""
        text = json.dumps(self.manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        return write_package_dir(dest, text, self.files)

    def __repr__(self) -> str:
        label = f"{self._raw.get('name')!r}" if isinstance(self._raw, dict) else "unreadable manifest"
        return f"<ToolPackage {label}, {len(self.files)} files>"

    # -- validation ----------------------------------------------------------------------------------------------------

    def validate(self, settings: Settings) -> ValidationReport:
        """Check every rule without running anything. Never raises on bad content; unexpected failure rejects the package."""
        out = _Collector()
        try:
            self._validate(out, settings)
        except Exception as exc:
            out.add(
                "package.internal_error",
                "package",
                f"validation stopped unexpectedly ({type(exc).__name__}); the package is treated as invalid",
            )
        return out.report()

    def _validate(self, out: _Collector, settings: Settings) -> None:
        for path, code, message in self._problems:
            out.add(code, path, message)
        limit = settings.max_package_kb * 1024
        manifest, manifest_bytes = self._check_manifest(out, limit)
        if manifest and manifest.kind in ("stateless", "session") and manifest.entrypoint is None:
            out.add("field.required", "entrypoint", "entrypoint is required for stateless or session kinds")
        trees = self._check_files(out, limit, manifest_bytes)
        self._check_entrypoint(out, trees)
        schema_ok = self._check_schemas(out)
        if manifest is not None:
            self._check_examples(out, manifest, schema_ok)
            self._check_relations(out, manifest)
            self._check_advice(out, manifest)

    def _check_manifest(self, out: _Collector, limit: int) -> tuple[Manifest | None, int]:
        raw = self._raw
        if raw is None:
            if not self._manifest_explained:
                out.add("manifest.missing", "manifest.json/yaml/yml", "the package has no manifest")
            return None, 0
        if not isinstance(raw, dict):
            out.add(
                "manifest.not_object", "manifest", f"the manifest must be an object, got {type(raw).__name__}"
            )
            return None, 0
        size = len(canonical_manifest_bytes(raw))
        if size > limit:
            out.add(
                "package.too_large",
                "manifest",
                f"the manifest alone is {size // 1024} KB; the limit is {limit // 1024} KB",
            )
            return None, size
        try:
            return self._parsed or Manifest.model_validate(raw), size
        except PydanticValidationError as exc:
            for error in exc.errors(include_url=False):
                self._add_model_error(out, error)
            return None, size

    @staticmethod
    def _add_model_error(out: _Collector, error: Any) -> None:
        loc = tuple(error["loc"])
        path = _loc_path(loc) or "manifest"
        kind = error["type"]
        if kind in ISSUE_CODES:  # raised by one of the rules above, so the code and message are already ours
            out.add(kind, path, error["msg"])
        elif kind == "missing":
            out.add("field.required", path, f"the required field {path!r} is missing")
        elif kind == "extra_forbidden":
            out.add("field.unknown", path, f"{path!r} is not a field of the manifest")
        else:
            out.add("field.type", path, f"{error['msg']} (got {type(error.get('input')).__name__})")

    def _check_files(self, out: _Collector, limit: int, manifest_bytes: int) -> dict[str, ast.Module | str]:
        trees: dict[str, ast.Module | str] = {}
        if len(self.files) > MAX_FILES:
            out.add(
                "files.too_many",
                "files",
                f"the package has {len(self.files)} files; the most allowed is {MAX_FILES}",
            )
            # Do not look inside: a partial scan would report misleading follow-on issues (such as missing tests), and
            # a hostile submission could make the scan long. One clear problem is more useful.
            return trees
        total = manifest_bytes
        for path, text in sorted(self.files.items(), key=lambda item: str(item[0])):
            label = _safe(str(path))
            problem = check_rel_path(path)
            if problem is None and path in MANIFEST_NAMES:
                problem = "is the manifest itself; send it as the manifest, not as a file"
            if problem:
                out.add("file.path", label, f"the path {problem}")
                continue
            if not isinstance(text, str):
                out.add("file.type", label, f"the content must be text, got {type(text).__name__}")
                continue
            suffix = PurePosixPath(path).suffix
            if suffix not in ALLOWED_EXTENSIONS:
                out.add(
                    "file.extension",
                    label,
                    f"the extension {suffix or '(none)'} is not allowed; use one of {_EXT}",
                )
            if len(text) > MAX_FILE_BYTES:  # characters never outnumber bytes, so this is already too large
                out.add("file.too_large", label, f"the file has more than {MAX_FILE_BYTES // 1024} KB")
                continue
            bad = text_problem(text)
            if bad:
                out.add(bad[0], label, f"the file {bad[1]}")
                continue
            size = len(text.encode("utf-8"))
            total += size
            if size > MAX_FILE_BYTES:
                out.add(
                    "file.too_large",
                    label,
                    f"the file is {size // 1024} KB; the most allowed is {MAX_FILE_BYTES // 1024} KB",
                )
            if suffix == ".py":
                tree = _parse_python(text)
                trees[path] = tree
                if isinstance(tree, str):
                    out.add("file.syntax", label, f"does not parse as Python: {tree}")
        if total > limit:
            out.add(
                "package.too_large",
                "package",
                f"the package is {total // 1024} KB; the limit is {limit // 1024} KB",
            )
        count = sum(
            1
            for path, tree in trees.items()
            if TEST_FILE.fullmatch(path) and isinstance(tree, ast.Module)
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        )
        if count < MIN_TESTS:
            out.add(
                "tests.too_few",
                "tests",
                f"found {count} top-level test functions in tests/test_*.py; at least {MIN_TESTS} are needed",
            )
        return trees

    def _check_entrypoint(self, out: _Collector, trees: dict[str, ast.Module | str]) -> None:
        raw = self._raw.get("entrypoint") if isinstance(self._raw, dict) else None
        if not isinstance(raw, str) or entrypoint_problem(raw) is not None:
            return  # absent or malformed: the manifest rules above already reported it
        path, function = raw.split(":")
        if path in self._skipped:
            return  # the file was unreadable; that is already an issue
        if path not in self.files:
            out.add("entrypoint.missing_file", "entrypoint", f"{path} is not in the package")
            return
        tree = trees.get(path)
        if not isinstance(tree, ast.Module):
            return  # a syntax error (reported as file.syntax) or a file that failed another rule
        defined = [node for node in _top_level_functions(tree) if node.name == function]
        if any(isinstance(node, ast.FunctionDef) for node in defined):
            return
        if defined:
            message = f"{path} defines {function} with async def; define it with plain def, because it is called directly"
        else:
            message = f"{path} has no top-level function named {function} (not inside a class or an if block, not an assignment)"
        out.add("entrypoint.missing_function", "entrypoint", message)

    def _check_schemas(self, out: _Collector) -> dict[str, bool]:
        """Schema rules run on the raw manifest, so they report even when another field is broken."""
        status = {"input_schema": False, "output_schema": False}
        raw = self._raw if isinstance(self._raw, dict) else {}
        for name in status:
            schema = raw.get(name)
            if isinstance(schema, dict):
                status[name] = self._check_schema(out, name, schema, is_input=name == "input_schema")
        return status

    @staticmethod
    def _check_schema(out: _Collector, name: str, schema: dict[str, Any], *, is_input: bool) -> bool:
        problem = _jsonschema_problem(schema)
        if problem:
            out.add("schema.invalid", name, f"{name} is not valid JSON Schema (Draft 2020-12): {problem}")
            return False
        scan = _SchemaScan()
        _scan_schema(schema, name, 1, scan)
        before = len(out.errors)
        for path, ref in scan.remote:
            out.add(
                "schema.remote_ref",
                path,
                f"{_clip(ref)} is a remote reference; only local references starting with # are allowed",
            )
        for path, ref in scan.local:
            if not _ref_resolves(schema, ref):
                out.add(
                    "schema.invalid",
                    path,
                    f"{name} refers to {_clip(ref)}, which does not exist in the schema",
                )
        if scan.max_depth > MAX_SCHEMA_DEPTH:
            out.add(
                "schema.too_deep", name, f"{name} nests subschemas more than {MAX_SCHEMA_DEPTH} levels deep"
            )
        if scan.properties > MAX_SCHEMA_PROPERTIES:
            out.add(
                "schema.too_many_properties",
                name,
                f"{name} declares {scan.properties} properties; the most allowed is {MAX_SCHEMA_PROPERTIES}",
            )
        if is_input:
            if schema.get("type") != "object":
                out.add("schema.not_object", name, f"{name} must have top-level type object")
            if schema.get("additionalProperties") is not False:
                out.add(
                    "schema.additional_properties",
                    name,
                    f"{name} must set top-level additionalProperties to false",
                )
        return len(out.errors) == before

    def _check_examples(self, out: _Collector, manifest: Manifest, schema_ok: dict[str, bool]) -> None:
        for index, example in enumerate(manifest.examples):
            self._check_example_files(out, index, example)
        checks: list[tuple[dict[str, Any], list[Any]]] = []
        targets: list[tuple[str, list[int]]] = []
        if schema_ok["input_schema"]:
            checks.append((manifest.input_schema, [example.input for example in manifest.examples]))
            targets.append(("input", list(range(len(manifest.examples)))))
        if manifest.output_schema is not None and schema_ok["output_schema"]:
            given = [i for i, example in enumerate(manifest.examples) if example.output is not None]
            checks.append((manifest.output_schema, [manifest.examples[i].output for i in given]))
            targets.append(("output", given))
        if not checks:
            return
        try:
            results = check_instances(
                checks, isolated=SCHEMA_CHECK_ISOLATED, timeout_s=SCHEMA_CHECK_TIMEOUT_S
            )
        except SchemaCheckError as exc:
            out.add(
                "schema.too_expensive", "examples", f"checking the examples against the schemas failed: {exc}"
            )
            return
        for (kind, indices), reasons in zip(targets, results, strict=True):
            for index, reason in zip(indices, reasons, strict=True):
                if reason is not None:
                    out.add(
                        f"examples.{kind}_invalid",
                        f"examples[{index}].{kind}",
                        f"the example {kind} does not match {kind}_schema: {reason}",
                    )

    @staticmethod
    def _check_example_files(out: _Collector, index: int, example: Example) -> None:
        if len(example.files) > MAX_FILES:
            out.add("examples.file_invalid", f"examples[{index}].files", f"has more than {MAX_FILES} files")
            return
        for key, content in example.files.items():
            path = f"examples[{index}].files[{_safe(key, 60)}]"
            problem = check_rel_path(key)
            if problem is None and not key.startswith("inputs/"):
                problem = "must start with inputs/"
            if problem is None and len(content) > MAX_FILE_BYTES:
                problem = f"has more than {MAX_FILE_BYTES // 1024} KB of content"
            if problem is None and (bad := text_problem(content)):
                problem = f"has content that {bad[1]}"
            if problem:
                out.add("examples.file_invalid", path, f"the example file name {problem}")

    @staticmethod
    def _check_relations(out: _Collector, manifest: Manifest) -> None:
        if manifest.parent_version is None:
            return
        parent, current = semver_tuple(manifest.parent_version), semver_tuple(manifest.version)
        if parent is not None and current is not None and parent >= current:
            out.add(
                "parent_version.not_lower",
                "parent_version",
                f"parent_version {manifest.parent_version} must be lower than version {manifest.version}",
            )

    @staticmethod
    def _check_advice(out: _Collector, manifest: Manifest) -> None:
        if not manifest.when_to_use:
            out.add("when_to_use.empty", "when_to_use", "when_to_use is empty")
        if manifest.output_schema is None:
            out.add("output_schema.missing", "output_schema", "the manifest has no output_schema")
        if all(example.output is None for example in manifest.examples):
            out.add("examples.no_output", "examples", "no example gives an expected output")
