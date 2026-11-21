"""Boundary inputs for a tool, generated from its `input_schema` and its examples. No model, no network, fully deterministic.

Gate G4 (fuzz) feeds these to the tool. Two sources, mixed so that a small budget still covers every property:

* **Boundary values per property**: empty and very long strings, unicode, the edges of a numeric range and a few steps past
  zero, both booleans, `null`, empty and full arrays, every `enum` member.
* **The manifest examples, changed one property at a time**: each example with one property replaced by a boundary value, or
  left out when it is optional. The example's own files travel with it.

The same `(schema, examples, seed)` always gives the same list: the seed (G4 uses the package's `code_sha`) only decides which of
the candidates are kept when there are more than `count`, and in which order within a property. Every returned input is checked
against the schema by `schemacheck` (isolated by default, because the schema is hostile input): a candidate that does not validate
is dropped, so "every generated input validates" holds by construction and is also asserted by `tests/test_schemagen.py`.

Limits, written down: `pattern`, `format`, `multipleOf`, `not` and `if/then/else` are not used to build values, only to reject
them afterwards, so a schema that is mostly such constraints yields fewer inputs; the examples (which already validate) then carry
the run. Nothing here proves a tool correct; it looks for crashes and hangs on inputs the author may not have tried.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from toolforge import schemacheck

DEFAULT_COUNT = 30
LONG_STRING_CHARS = 10_000  # "a long string up to a cap"; shortened to `maxLength` when the schema says less
UNICODE_SAMPLE = "héllo ✓ 日本 \U0001f600"
MAX_DEPTH = 5  # nested schemas below this depth yield one typical value only
MAX_ARRAY_ITEMS = 20  # the longest array built from nothing but a schema (or `maxItems` if smaller)
MAX_ENUM_MEMBERS = 20
_VALUES_PER_SCHEMA = 10  # boundary values kept per subschema, typical value first
_POOL_FACTOR = 4  # candidates built per wanted input, since some will not validate
_MAX_POOL = 800
_LARGE_INT = 2**53
_TYPICAL_TEXT = "hello world"


@dataclass(frozen=True)
class GeneratedInput:
    """One input for the tool. `label` says in a few words what is unusual about it (shown in hints, never executed)."""

    args: dict[str, Any]
    files: dict[str, str]
    label: str


def describe_value(value: Any) -> str:
    """A short plain description of a value for a label: never the whole text of a long string."""
    if isinstance(value, str):
        if value == "":
            return "an empty string"
        if len(value) > 40:
            return f"a string of {len(value)} characters"
        return repr(value)
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (int, float)):
        text = repr(value)
        return text if len(text) <= 24 else f"a number with {len(text)} digits"
    if isinstance(value, list):
        return f"a list of {len(value)}"
    if isinstance(value, dict):
        return f"an object with {len(value)} keys"
    return type(value).__name__


def _unique(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        try:
            key = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _number(schema: Mapping[str, Any], key: str) -> float | None:
    value = schema.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _clamp(value: float, low: float | None, high: float | None) -> float:
    if low is not None and value < low:
        value = low
    if high is not None and value > high:
        value = high
    return value


def _fit(text: str, minimum: int, maximum: int | None) -> str:
    if len(text) < minimum:
        text = text + "x" * (minimum - len(text))
    return text if maximum is None else text[:maximum]


def _int_bound(schema: Mapping[str, Any], which: str) -> int | None:
    plain, exclusive = ("minimum", "exclusiveMinimum") if which == "low" else ("maximum", "exclusiveMaximum")
    bounds: list[int] = []
    value = _number(schema, plain)
    if value is not None:
        bounds.append(int(-(-value // 1)) if which == "low" else int(value // 1))
    value = _number(schema, exclusive)
    if value is not None:
        bounds.append(int(value // 1) + 1 if which == "low" else int(-(-value // 1)) - 1)
    if not bounds:
        return None
    return max(bounds) if which == "low" else min(bounds)


def _float_bound(schema: Mapping[str, Any], which: str) -> float | None:
    plain, exclusive = ("minimum", "exclusiveMinimum") if which == "low" else ("maximum", "exclusiveMaximum")
    bounds: list[float] = []
    value = _number(schema, plain)
    if value is not None:
        bounds.append(float(value))
    value = _number(schema, exclusive)
    if value is not None:
        step = 1e-6
        bounds.append(float(value) + step if which == "low" else float(value) - step)
    if not bounds:
        return None
    return max(bounds) if which == "low" else min(bounds)


def _round_robin(groups: Sequence[Sequence[Any]]) -> list[Any]:
    """First of every group, then second of every group, and so on, so no group starves the others."""
    out: list[Any] = []
    depth = max((len(group) for group in groups), default=0)
    for level in range(depth):
        out.extend(group[level] for group in groups if level < len(group))
    return out


class _Generator:
    def __init__(self, root: Mapping[str, Any], rng: random.Random) -> None:
        self.root = root
        self.rng = rng

    # -- references --------------------------------------------------------------------------------------------------------

    def _resolve(self, ref: str) -> Any:
        if not ref.startswith("#"):
            return None
        node: Any = self.root
        for part in [p for p in ref[1:].split("/") if p]:
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return None
        return node

    # -- values of one subschema -------------------------------------------------------------------------------------------

    def values(self, schema: Any, depth: int = 0) -> list[Any]:
        """A few boundary values for `schema`, the most ordinary one first. May include values the schema rejects."""
        if depth > MAX_DEPTH:
            return []
        if schema is True or schema == {}:
            return [_TYPICAL_TEXT, "", 0, None, [], {}]
        if not isinstance(schema, Mapping):
            return []
        if "$ref" in schema:
            target = self._resolve(str(schema["$ref"]))
            return self.values(target, depth + 1) if target is not None else []
        if "const" in schema:
            return [schema["const"]]
        if isinstance(schema.get("enum"), list):
            return _unique(schema["enum"][:MAX_ENUM_MEMBERS])
        for combiner in ("anyOf", "oneOf", "allOf"):
            branches = schema.get(combiner)
            if isinstance(branches, list) and branches:
                merged: list[Any] = []
                for branch in branches[:6]:
                    merged.extend(self.values(branch, depth + 1)[:4])
                return _unique(merged)[:_VALUES_PER_SCHEMA]
        kinds = schema.get("type")
        if isinstance(kinds, str):
            kinds = [kinds]
        if not isinstance(kinds, list):
            if "properties" in schema or "required" in schema:
                kinds = ["object"]
            elif "items" in schema:
                kinds = ["array"]
            else:
                return self.values({}, depth)
        out: list[Any] = []
        for kind in kinds:
            builder = getattr(self, f"_{kind}", None) if isinstance(kind, str) else None
            if builder is not None:
                out.extend(builder(schema, depth))
        return _unique(out)[:_VALUES_PER_SCHEMA]

    def _string(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        low = max(int(_number(schema, "minLength") or 0), 0)
        high_value = _number(schema, "maxLength")
        high = None if high_value is None else max(int(high_value), 0)
        if high is not None and high < low:
            return []
        candidates = [
            _fit(_TYPICAL_TEXT, low, high),
            "" if low == 0 else _fit("", low, high),
            _fit(" " * 3, low, high),
            _fit("x" * LONG_STRING_CHARS, low, high),
            _fit(UNICODE_SAMPLE, low, high),
            _fit("line one\nline two\ttabbed", low, high),
        ]
        if high is not None:
            candidates.append("y" * high)
        return _unique(candidates)

    def _integer(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        low, high = _int_bound(schema, "low"), _int_bound(schema, "high")
        if low is not None and high is not None and low > high:
            return []
        wanted: list[int] = [1, 0, -1, 2, 2**31 - 1, -(2**31), _LARGE_INT]
        if low is not None:
            wanted += [low, low + 1]
        if high is not None:
            wanted += [high, high - 1]
        if low is not None and high is not None:
            wanted.append((low + high) // 2)
        typical = int(_clamp(1, low, high))
        clamped = [int(_clamp(value, low, high)) for value in wanted]
        return _unique([typical, *clamped])

    def _number(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        low, high = _float_bound(schema, "low"), _float_bound(schema, "high")
        if low is not None and high is not None and low > high:
            return []
        wanted: list[float] = [1.5, 0.0, -1.5, 1e-9, 1e15, 3.141592653589793, 1e300, -1e300]
        if low is not None:
            wanted.append(low)
        if high is not None:
            wanted.append(high)
        if low is not None and high is not None:
            wanted.append((low + high) / 2)
        typical = float(_clamp(1.5, low, high))
        values = [typical] + [float(_clamp(value, low, high)) for value in wanted]
        return (
            _unique(values)
            + _unique(int(v) for v in values if float(v).is_integer() and abs(v) < _LARGE_INT)[:2]
        )

    def _boolean(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        return [True, False]

    def _null(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        return [None]

    def _array(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        low = max(int(_number(schema, "minItems") or 0), 0)
        high_value = _number(schema, "maxItems")
        high = MAX_ARRAY_ITEMS if high_value is None else min(max(int(high_value), 0), MAX_ARRAY_ITEMS)
        if high < low:
            return []
        items = schema.get("items", True)
        if depth + 1 > MAX_DEPTH:
            return [[]] if low == 0 else []
        item_values = self.values(items, depth + 1) or [None]
        unique = schema.get("uniqueItems") is True

        def build(size: int, pool: list[Any]) -> list[Any]:
            if unique:
                return pool[:size]
            return [pool[n % len(pool)] for n in range(size)]

        typical_size = max(low, 1) if high >= 1 else 0
        out = [
            build(typical_size, item_values[:1]) if not unique else build(typical_size, item_values),
            [] if low == 0 else build(low, item_values),
            build(high, item_values),
            build(min(high, max(low, 5)), item_values[:5]),
        ]
        return _unique(out)

    def _object(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        structural, groups = self.object_variants(schema, depth)
        flat = [args for args, _ in structural] + [args for args, _ in _round_robin(groups)]
        return _unique(flat)[: _VALUES_PER_SCHEMA - 4]

    # -- objects, with a reason for every variant ---------------------------------------------------------------------------

    def object_variants(
        self, schema: Mapping[str, Any], depth: int
    ) -> tuple[list[tuple[dict[str, Any], str]], list[list[tuple[dict[str, Any], str]]]]:
        """`(structural variants, one group of variants per property)`; each variant is `(value, label)`."""
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required_list = schema.get("required")
        required = (
            [name for name in required_list if isinstance(name, str)]
            if isinstance(required_list, list)
            else []
        )
        values = {
            str(name): self.values(sub, depth + 1) if depth + 1 <= MAX_DEPTH else []
            for name, sub in properties.items()
        }
        typical = {name: options[0] for name, options in values.items() if options}
        only_required = {name: typical.get(name, _TYPICAL_TEXT) for name in required}
        everything = {**typical, **only_required}
        structural: list[tuple[dict[str, Any], str]] = [(only_required, "only the required properties")]
        if everything != only_required:
            structural.append((everything, "every property present"))
        groups: list[list[tuple[dict[str, Any], str]]] = []
        for name, options in values.items():
            group = [
                ({**only_required, name: value}, f"{name} = {describe_value(value)}") for value in options[1:]
            ]
            self.rng.shuffle(group)
            if group:
                groups.append(group)
        self.rng.shuffle(groups)
        return structural, groups

    # -- the whole input ---------------------------------------------------------------------------------------------------

    def boundary_inputs(self) -> list[GeneratedInput]:
        root = self.root if isinstance(self.root, Mapping) else {}
        structural, groups = self.object_variants(root, 0)
        found = [GeneratedInput(args, {}, label) for args, label in structural]
        found += [GeneratedInput(args, {}, label) for args, label in _round_robin(groups)]
        return found

    def mutated_examples(self, examples: Sequence[Any]) -> list[GeneratedInput]:
        root = self.root if isinstance(self.root, Mapping) else {}
        properties = root.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required_list = root.get("required")
        required = (
            {name for name in required_list if isinstance(name, str)}
            if isinstance(required_list, list)
            else set()
        )
        groups: list[list[GeneratedInput]] = []
        for number, example in enumerate(examples, start=1):
            base = _example_field(example, "input")
            if not isinstance(base, dict) or _example_field(example, "live") is True:
                continue
            raw_files = _example_field(example, "files")
            files = dict(raw_files) if isinstance(raw_files, dict) else {}
            for name, sub in properties.items():
                changes: list[GeneratedInput] = []
                for value in self.values(sub, 1)[1:]:
                    if base.get(name) == value:
                        continue
                    changes.append(
                        GeneratedInput(
                            {**base, name: value},
                            files,
                            f"example {number} with {name} = {describe_value(value)}",
                        )
                    )
                if name not in required and name in base:
                    changes.append(
                        GeneratedInput(
                            {k: v for k, v in base.items() if k != name},
                            files,
                            f"example {number} without {name}",
                        )
                    )
                self.rng.shuffle(changes)
                if changes:
                    groups.append(changes)
        self.rng.shuffle(groups)
        return _round_robin(groups)


def _example_field(example: Any, name: str) -> Any:
    return example.get(name) if isinstance(example, Mapping) else getattr(example, name, None)


def _canonical(item: GeneratedInput) -> str:
    try:
        return json.dumps([item.args, item.files], sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        return repr(id(item))


def isolated_validator(schema: Mapping[str, Any]) -> Callable[[list[dict[str, Any]]], list[bool]]:
    """Validate candidates against `schema` in the isolated checker (a hostile `pattern` cannot stall the host)."""

    def validate(candidates: list[dict[str, Any]]) -> list[bool]:
        if not candidates:
            return []
        answers = schemacheck.check_instances([(schema, candidates)])[0]
        return [answer is None for answer in answers]

    return validate


def generate(
    schema: Mapping[str, Any],
    examples: Sequence[Any] = (),
    *,
    seed: str | int,
    count: int = DEFAULT_COUNT,
    validate: Callable[[list[dict[str, Any]]], list[bool]] | None = None,
) -> list[GeneratedInput]:
    """Up to `count` distinct inputs that all validate against `schema`, boundary values and mutated examples interleaved.

    `examples` are the manifest's examples (objects with `input`, `files` and `live`, or plain dicts); `live` ones are not
    mutated. `validate` maps candidate inputs to "does it validate"; the default is the isolated `schemacheck`. Raises
    `schemacheck.SchemaCheckError` when the schema cannot be checked in time.
    """
    if count <= 0 or not isinstance(schema, Mapping):
        return []
    rng = random.Random(str(seed))
    generator = _Generator(schema, rng)
    structural = generator.boundary_inputs()
    mutated = generator.mutated_examples(examples)
    head, tail = structural[:2], structural[2:]
    mixed: list[GeneratedInput] = list(head)
    for index in range(max(len(tail), len(mutated))):
        if index < len(tail):
            mixed.append(tail[index])
        if index < len(mutated):
            mixed.append(mutated[index])
    pool: list[GeneratedInput] = []
    seen: set[str] = set()
    for item in mixed:
        key = _canonical(item)
        if key not in seen:
            seen.add(key)
            pool.append(item)
        if len(pool) >= min(count * _POOL_FACTOR, _MAX_POOL):
            break
    check = validate or isolated_validator(schema)
    verdicts = check([item.args for item in pool])
    valid = [item for item, ok in zip(pool, verdicts, strict=True) if ok]
    return valid[:count]


__all__ = [
    "DEFAULT_COUNT",
    "LONG_STRING_CHARS",
    "GeneratedInput",
    "describe_value",
    "generate",
    "isolated_validator",
]
