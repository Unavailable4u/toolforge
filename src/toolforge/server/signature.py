"""Typed call signatures for `forge_describe` (P12): the exact shape of a call as a Python and a TypeScript stub, so a model that writes
code which calls a tool does not have to read a JSON Schema.

**The schema is hostile input from a tool author, so no author text is ever copied into a code position.** What this module does about
it, each point proven in `tests/test_signature.py`:

* A property becomes a named parameter only if its name matches `[A-Za-z_][A-Za-z0-9_]{0,63}` *entirely* (`re.fullmatch`, so a trailing
  newline does not slip through), does not start with two underscores and is not a Python or TypeScript keyword. If any property fails
  that, the stub takes one `args` object instead and a note says why. The same test decides the function name (`tool_` is put in front
  of a name that is a keyword).
* Descriptions, titles, examples and every other prose field never appear: no comments, no docstrings. Notes are fixed phrases that name
  a *construct* (`oneOf`), never a property or a value.
* An enum value, and a default, is emitted only as a JSON-escaped string literal of at most 40 printable BMP characters that
  `textsafety.sanitize` leaves unchanged, and an enum of at most 12 values. Anything else makes the field its plain type (or `...` for a
  default) and the status `partial`.
* Each stub is at most 2000 characters; a longer one is replaced by the plain `args` form and the status is `partial`.
* The output is a pure function of the manifest's two schemas: the same manifest gives byte-identical stubs.

A stub **describes** how to call a tool. It is not an importable module and it runs nothing: tools run only through `forge_run`. It is built
from `input_schema` and `output_schema` alone, and a construct it cannot translate (`oneOf`, `$ref`, `patternProperties`, a schema in
`additionalProperties`, `format`, a type list) becomes `Any` / `unknown` with status `partial`: a stub is never guessed.
"""

from __future__ import annotations

import json
import keyword
import re
from dataclasses import dataclass, field
from typing import Any

from toolforge import textsafety

MAX_STUB_CHARS = 2000
MAX_DEPTH = 4
MAX_ENUM_VALUES = 12
MAX_LITERAL_CHARS = 40
MAX_NOTES = 5
STATUSES = ("generated", "partial", "not_available")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_PYTHON_RESERVED = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist)
_TS_RESERVED = frozenset(
    "abstract any as asserts async await boolean break case catch class const constructor continue debugger declare default delete do "
    "else enum export extends false finally for from function get if implements import in infer instanceof interface is keyof let "
    "module namespace never new null number object of package private protected public readonly require return set static string "
    "super switch symbol this throw true try type typeof undefined unknown var void while with yield".split()
)
# Constructs that make a node `Any` / `unknown`, in the order they are looked for. `anyOf` is handled before this list.
_UNTRANSLATED = ("$ref", "oneOf", "allOf", "not", "if", "patternProperties", "prefixItems", "format")
_SCALARS = {
    "string": ("str", "string"),
    "integer": ("int", "number"),
    "number": ("float", "number"),
    "boolean": ("bool", "boolean"),
    "null": ("None", "null"),
}


def is_plain_name(name: object) -> bool:
    """True for a name that may appear as a parameter, property or function name in a stub."""
    return (
        isinstance(name, str)
        and _IDENTIFIER.fullmatch(name) is not None
        and not name.startswith("__")
        and name not in _PYTHON_RESERVED
        and name not in _TS_RESERVED
    )


def safe_literal(value: object) -> str | None:
    """A JSON-escaped string literal for a short plain string, else None. Valid in Python and in TypeScript."""
    if not isinstance(value, str) or not value or len(value) > MAX_LITERAL_CHARS:
        return None
    if not value.isprintable() or any(ord(char) > 0xFFFF for char in value):
        return None
    if textsafety.sanitize(value, max_chars=MAX_LITERAL_CHARS + 20) != value:
        return None
    return json.dumps(value, ensure_ascii=True)


@dataclass
class _Run:
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.notes and len(self.notes) < MAX_NOTES:
            self.notes.append(text)

    @property
    def partial(self) -> bool:
        return bool(self.notes)


Type = tuple[str, str]  # (Python, TypeScript)
ANY: Type = ("Any", "unknown")


def _nullable(types: Type) -> Type:
    py, ts = types
    return (
        py if py == "None" or py.endswith("| None") or py == "Any" else f"{py} | None",
        ts if ts in ("null", "unknown") or ts.endswith("| null") else f"{ts} | null",
    )


def _array_of(item: Type) -> Type:
    py, ts = item
    return (f"list[{py}]", f"({ts})[]" if "|" in ts else f"{ts}[]")


def _enum(values: list[Any], base: str, run: _Run) -> Type | None:
    """A literal union for an enum of short plain strings; None (with a note) when it cannot be one."""
    literals = [safe_literal(value) for value in values]
    if (
        base != "string"
        or not values
        or len(values) > MAX_ENUM_VALUES
        or any(item is None for item in literals)
    ):
        run.note("an enum that is not a short list of short plain strings is typed as its base type")
        return None
    unique = list(dict.fromkeys(item for item in literals if item is not None))
    return (f"Literal[{', '.join(unique)}]", " | ".join(unique))


def _object(schema: dict[str, Any], depth: int, run: _Run) -> Type:
    if isinstance(schema.get("additionalProperties"), dict):
        run.note("additionalProperties with a schema is not translated (typed as Any)")
        return ANY
    properties = schema.get("properties")
    plain = ("dict[str, Any]", "Record<string, unknown>")
    if not isinstance(properties, dict) or not properties:
        return plain
    if not all(is_plain_name(name) for name in properties):
        run.note(
            "a nested property name is not a plain identifier, so that object is typed as a plain record"
        )
        return plain
    required = set(schema.get("required") or []) if isinstance(schema.get("required"), list) else set()
    fields = []
    for name, sub in properties.items():
        fields.append(f"{name}{'' if name in required else '?'}: {_node(sub, depth + 1, run)[1]}")
    return ("dict[str, Any]", "{ " + "; ".join(fields) + " }")


def _node(schema: Any, depth: int, run: _Run) -> Type:
    """The (Python, TypeScript) type of one schema node. Anything it cannot translate is `Any` / `unknown` and leaves a note."""
    if not isinstance(schema, dict):
        run.note("a schema that is not an object is not translated (typed as Any)")
        return ANY
    if depth > MAX_DEPTH:
        run.note(f"nesting deeper than {MAX_DEPTH} levels is not translated (typed as Any)")
        return ANY
    for construct in _UNTRANSLATED:
        if construct in schema:
            run.note(f"{construct} is not translated (typed as Any)")
            return ANY
    if "anyOf" in schema:
        branches = schema["anyOf"]
        others = [b for b in branches if b != {"type": "null"}] if isinstance(branches, list) else []
        if isinstance(branches, list) and len(branches) == 2 and len(others) == 1:
            return _nullable(_node(others[0], depth, run))
        run.note("anyOf other than a type or null is not translated (typed as Any)")
        return ANY
    kind = schema.get("type")
    if isinstance(kind, list):
        rest = [k for k in kind if k != "null"]
        if len(kind) == 2 and len(rest) == 1 and isinstance(rest[0], str):
            return _nullable(_node({**schema, "type": rest[0]}, depth, run))
        run.note("a type list other than a type or null is not translated (typed as Any)")
        return ANY
    if kind is None:
        return ANY
    if kind == "object":
        return _object(schema, depth, run)
    if kind == "array":
        items = schema.get("items")
        return (
            _array_of(_node(items, depth + 1, run)) if isinstance(items, dict) else ("list[Any]", "unknown[]")
        )
    scalar = _SCALARS.get(kind) if isinstance(kind, str) else None
    if scalar is None:
        run.note("a type that JSON Schema does not define is not translated (typed as Any)")
        return ANY
    if isinstance(schema.get("enum"), list):
        literal = _enum(schema["enum"], kind, run)
        if literal is not None:
            return literal
    return scalar


def _default(value: object) -> str | None:
    """A default as a Python literal, or None when it cannot be written safely."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value) if abs(value) < 10**15 else None
    if isinstance(value, float):
        return repr(value) if value == value and abs(value) < 1e15 else None  # noqa: PLR0124  (NaN is not equal to itself)
    return safe_literal(value)


def _plain_forms(name: str, output: Type) -> tuple[str, str]:
    return (
        f"def {name}(args: dict[str, Any]) -> {output[0]}: ...",
        f"declare function {name}(args: Record<string, unknown>): Promise<{output[1]}>;",
    )


def _function_name(name: object, run: _Run) -> str:
    text = name if isinstance(name, str) else "tool"
    if is_plain_name(text):
        return text
    run.note("the tool name is a reserved word, so the function is called tool_<name>")
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", text)[:60] or "call"
    return f"tool_{cleaned}"


def build_signature(manifest: dict[str, Any]) -> dict[str, Any]:
    """`{"python", "typescript", "status", "notes"}` for a manifest. See the module docstring for every rule."""
    schema = manifest.get("input_schema") if isinstance(manifest, dict) else None
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {
            "python": None,
            "typescript": None,
            "status": "not_available",
            "notes": ["no usable input_schema"],
        }
    run = _Run()
    name = _function_name(manifest.get("name"), run)
    output_schema = manifest.get("output_schema")
    if isinstance(output_schema, dict):
        output = _node(output_schema, 0, run)
    else:
        run.note("no output_schema, so the result is typed as Any")
        output = ANY
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    if not all(is_plain_name(prop) for prop in properties):
        run.note("a property name is not a plain identifier, so the call takes one `args` object")
        py, ts = _plain_forms(name, output)
    else:
        names = list(properties)
        ordered = [n for n in names if n in required] + [n for n in names if n not in required]
        py_params: list[str] = []
        ts_params: list[str] = []
        for prop in ordered:
            sub = properties[prop]
            py_type, ts_type = _node(sub, 1, run)
            if prop in required:
                py_params.append(f"{prop}: {py_type}")
                ts_params.append(f"{prop}: {ts_type}")
                continue
            default = _default(sub.get("default")) if isinstance(sub, dict) and "default" in sub else None
            if isinstance(sub, dict) and "default" in sub and default is None:
                run.note("a default that is not a short plain value is written as `...`")
                py_params.append(f"{prop}: {py_type} = ...")
            elif default is not None:
                py_params.append(f"{prop}: {py_type} = {default}")
            else:
                py_params.append(f"{prop}: {_nullable((py_type, ts_type))[0]} = None")
            ts_params.append(f"{prop}?: {ts_type}")
        py = f"def {name}({', '.join(py_params)}) -> {output[0]}: ..."
        ts = (
            f"declare function {name}(args: {{ {'; '.join(ts_params)} }}): Promise<{output[1]}>;"
            if ts_params
            else f"declare function {name}(): Promise<{output[1]}>;"
        )
    if len(py) > MAX_STUB_CHARS or len(ts) > MAX_STUB_CHARS:
        run.note(f"a stub longer than {MAX_STUB_CHARS} characters is replaced by the plain `args` form")
        plain_py, plain_ts = _plain_forms(name, ANY)
        py, ts = (
            (plain_py if len(py) > MAX_STUB_CHARS else py),
            (plain_ts if len(ts) > MAX_STUB_CHARS else ts),
        )
    return {
        "python": py,
        "typescript": ts,
        "status": "partial" if run.partial else "generated",
        "notes": list(run.notes),
    }


__all__ = ["MAX_STUB_CHARS", "STATUSES", "build_signature", "is_plain_name", "safe_literal"]
