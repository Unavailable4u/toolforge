"""The challenger's expectations: a closed vocabulary of checks that the host evaluates. Nothing a model writes is ever run.

A model's reply names a check as data, `{"kind": "equals", "value": ...}`. Parsing it builds one of eight pydantic models with
`extra="forbid"`, so a reply that carries an unknown kind, an extra field, or code in any field (`{"kind": "python", "code": ...}`)
is not a check and the case is dropped. Evaluation is plain Python over the result the sandbox reported (already read defensively by
`sandboxrun`) and cannot loop, import or open anything: paths are bounded, values are bounded, comparison is the one in G3.

| kind | passes when |
| --- | --- |
| `equals` | the call returned a result equal to `value` (numbers within 1e-9; `true` is not `1`; key order is ignored) |
| `json_path_equals` | the value at `path` (`a.b[0].c`) equals `value` |
| `type_is` | the value (at `path`, or the whole result) has JSON type `type`; `1` is an integer and `1.0` is a number |
| `keys_present` | the object (at `path`, or the whole result) has every key in `keys` |
| `numeric_close` | the number (at `path`, or the whole result) is within `tolerance` of `value` |
| `length_between` | the length of the string, list or object (at `path`, or the whole result) is between `min` and `max` |
| `no_error` | the call returned a result instead of an error |
| `tool_error` | the tool refused on purpose with `ToolError` |

A `Crash`, `Timeout`, `OutputTooLarge` or `NotJson` is never an acceptable outcome of a schema-valid input; the gate treats those as
failures whatever the check says (see `verify/g5_challenger.py`). Proven by `tests/test_checks_dsl.py`.
"""

from __future__ import annotations

import json
import math
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from toolforge.verify.g3_selftest import json_equal
from toolforge.verify.pipeline import clip
from toolforge.verify.sandboxrun import ItemOutcome

MAX_PATH_CHARS = 200
MAX_PATH_STEPS = 12
MAX_VALUE_BYTES = 4096
MAX_KEYS = 50
MAX_NUMBER = 1e300

_STEP = re.compile(r"\.?([A-Za-z_][A-Za-z0-9_-]{0,63})|\[(\d{1,6})\]")

JSON_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


def _json_value(value: Any) -> Any:
    """A value that is JSON, small, and free of NaN and infinity: the only kind a check may compare against."""
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("value must be plain JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError(f"value must be at most {MAX_VALUE_BYTES} bytes of JSON")
    return value


def _path(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_PATH_CHARS:
        raise ValueError("path must be a short string")
    _steps(value)
    return value


def _steps(path: str) -> list[str | int]:
    steps: list[str | int] = []
    position = 0
    while position < len(path):
        match = _STEP.match(path, position)
        if match is None or (position == 0 and path[0] == "."):
            raise ValueError("path must look like a.b[0].c")
        steps.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
        position = match.end()
    if not steps or len(steps) > MAX_PATH_STEPS:
        raise ValueError(f"path must have 1 to {MAX_PATH_STEPS} steps")
    return steps


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Equals(_Base):
    kind: Literal["equals"]
    value: Any

    _v = field_validator("value")(_json_value)


class JsonPathEquals(_Base):
    kind: Literal["json_path_equals"]
    path: str
    value: Any

    _v = field_validator("value")(_json_value)

    @field_validator("path")
    @classmethod
    def _p(cls, value: str) -> str:
        return _path(value) or ""


class TypeIs(_Base):
    kind: Literal["type_is"]
    type: Literal["object", "array", "string", "integer", "number", "boolean", "null"]
    path: str | None = None

    _p = field_validator("path")(_path)


class KeysPresent(_Base):
    kind: Literal["keys_present"]
    keys: Annotated[list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=MAX_KEYS)]
    path: str | None = None

    _p = field_validator("path")(_path)


class NumericClose(_Base):
    kind: Literal["numeric_close"]
    value: Annotated[float, Field(allow_inf_nan=False, ge=-MAX_NUMBER, le=MAX_NUMBER)]
    tolerance: Annotated[float, Field(allow_inf_nan=False, ge=0, le=MAX_NUMBER)] = 1e-9
    path: str | None = None

    _p = field_validator("path")(_path)

    @field_validator("value", "tolerance", mode="before")
    @classmethod
    def _number(cls, value: Any) -> Any:
        # strict mode would refuse the integer 3 for a float field; JSON has one kind of number
        if isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        return value


class LengthBetween(_Base):
    kind: Literal["length_between"]
    min: Annotated[int, Field(ge=0, le=10**9)] | None = None
    max: Annotated[int, Field(ge=0, le=10**9)] | None = None
    path: str | None = None

    _p = field_validator("path")(_path)

    @model_validator(mode="after")
    def _order(self) -> LengthBetween:
        if self.min is None and self.max is None:
            raise ValueError("give min, max or both")
        if self.min is not None and self.max is not None and self.max < self.min:
            raise ValueError("max is below min")
        return self


class NoError(_Base):
    kind: Literal["no_error"]


class ToolErrorCheck(_Base):
    kind: Literal["tool_error"]


Check = Annotated[
    Equals | JsonPathEquals | TypeIs | KeysPresent | NumericClose | LengthBetween | NoError | ToolErrorCheck,
    Field(discriminator="kind"),
]
CHECK_KINDS = (
    "equals",
    "json_path_equals",
    "type_is",
    "keys_present",
    "numeric_close",
    "length_between",
    "no_error",
    "tool_error",
)
_ADAPTER: TypeAdapter[Any] = TypeAdapter(Check)


def parse_check(raw: Any) -> Check:
    """The check a model described, or `ValueError` when it is not one of the eight (unknown kind, extra field, bad value)."""
    try:
        return _ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise ValueError("not a valid check") from exc


# --- Evaluation ----------------------------------------------------------------------------------------------------------


class Verdict:
    """`ok` and, when not, a short host-written sentence saying what happened instead."""

    __slots__ = ("ok", "reason")

    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok, self.reason = ok, reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Verdict({self.ok!r}, {self.reason!r})"


_MISSING = object()


def _lookup(value: Any, path: str | None) -> Any:
    if path is None:
        return value
    for step in _steps(path):
        if isinstance(step, int):
            if not isinstance(value, list) or step >= len(value):
                return _MISSING
            value = value[step]
        else:
            if not isinstance(value, dict) or step not in value:
                return _MISSING
            value = value[step]
    return value


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _short(value: Any, limit: int = 80) -> str:
    try:
        return clip(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False), limit)
    except (TypeError, ValueError, RecursionError):
        return "(not JSON)"


def describe(check: Check) -> str:
    """The expectation in words, for a hint or a report."""
    where = f" at {check.path}" if getattr(check, "path", None) else ""
    if isinstance(check, Equals):
        return f"the result equals {_short(check.value)}"
    if isinstance(check, JsonPathEquals):
        return f"the value at {check.path} equals {_short(check.value)}"
    if isinstance(check, TypeIs):
        return f"the value{where} is of type {check.type}"
    if isinstance(check, KeysPresent):
        return f"the object{where} has the keys {', '.join(check.keys[:6])}"
    if isinstance(check, NumericClose):
        return f"the number{where} is within {check.tolerance:g} of {check.value:g}"
    if isinstance(check, LengthBetween):
        return f"the length{where} is between {check.min if check.min is not None else 0} and {check.max if check.max is not None else 'any'}"
    if isinstance(check, NoError):
        return "the call returns a result"
    return "the tool refuses on purpose with ToolError"


def evaluate(check: Check, outcome: ItemOutcome) -> Verdict:
    """Does the call's outcome meet the expectation? Only the host calls this, on data the sandbox reported."""
    if isinstance(check, ToolErrorCheck):
        if not outcome.ok and outcome.error_type == "ToolError":
            return Verdict(True)
        return Verdict(False, "it returned a result" if outcome.ok else f"it ended with {outcome.error_type}")
    if not outcome.ok:
        detail = (
            f"{outcome.error_type}: {clip(outcome.message, 90)}" if outcome.message else outcome.error_type
        )
        return Verdict(False, f"the call ended with {detail}")
    if isinstance(check, NoError):
        return Verdict(True)
    result = outcome.result
    if isinstance(check, Equals):
        return _verdict(json_equal(check.value, result), f"it returned {_short(result)}")
    target = _lookup(result, getattr(check, "path", None))
    if target is _MISSING:
        return Verdict(False, f"there is nothing at {check.path}")  # type: ignore[union-attr]
    if isinstance(check, JsonPathEquals):
        return _verdict(json_equal(check.value, target), f"the value at {check.path} is {_short(target)}")
    if isinstance(check, TypeIs):
        return _verdict(json_type(target) == check.type, f"it is of type {json_type(target)}")
    if isinstance(check, KeysPresent):
        if not isinstance(target, dict):
            return Verdict(False, f"it is of type {json_type(target)}, not an object")
        missing = [key for key in check.keys if key not in target]
        return _verdict(not missing, f"the keys {', '.join(missing[:6])} are missing")
    if isinstance(check, NumericClose):
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            return Verdict(False, f"it is of type {json_type(target)}, not a number")
        if isinstance(target, float) and not math.isfinite(target):
            return Verdict(False, "it is not a finite number")
        return _verdict(
            abs(target - check.value) <= check.tolerance + 1e-12 * abs(check.value), f"it is {target:g}"
        )
    if isinstance(check, LengthBetween):
        if not isinstance(target, (str, list, dict)):
            return Verdict(False, f"it is of type {json_type(target)}, which has no length")
        size = len(target)
        low = check.min if check.min is not None else 0
        ok = size >= low and (check.max is None or size <= check.max)
        return _verdict(ok, f"its length is {size}")
    return Verdict(False, "unknown check")  # unreachable: parse_check only builds the eight


def _verdict(ok: bool, reason: str) -> Verdict:
    return Verdict(True) if ok else Verdict(False, reason)


__all__ = [
    "CHECK_KINDS",
    "JSON_TYPES",
    "Check",
    "Verdict",
    "describe",
    "evaluate",
    "json_type",
    "parse_check",
]
