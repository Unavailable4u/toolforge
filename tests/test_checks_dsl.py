"""The challenger's check vocabulary: what parses, what is refused, and how each kind is judged."""

from __future__ import annotations

from typing import Any

import pytest

from toolforge.verify import checks_dsl
from toolforge.verify.checks_dsl import CHECK_KINDS, describe, evaluate, parse_check
from toolforge.verify.sandboxrun import ItemOutcome


def good(result: Any) -> ItemOutcome:
    return ItemOutcome(0, True, "", "", result, 10, 1)


def bad(kind: str, message: str = "boom") -> ItemOutcome:
    return ItemOutcome(0, False, kind, message, None, 0, 1)


RESULT = {
    "words": 3,
    "top": [{"word": "a", "count": 2}, {"word": "b", "count": 1}],
    "ratio": 0.5,
    "flag": True,
    "name": "x",
}


def passes(raw: dict[str, Any], outcome: ItemOutcome | None = None) -> bool:
    return evaluate(parse_check(raw), outcome or good(RESULT)).ok


def test_the_vocabulary_is_closed_and_has_eight_kinds() -> None:
    assert len(CHECK_KINDS) == 8 and len(set(CHECK_KINDS)) == 8


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "equals", "value": RESULT},
        {"kind": "json_path_equals", "path": "top[0].word", "value": "a"},
        {"kind": "type_is", "type": "integer", "path": "words"},
        {"kind": "type_is", "type": "number", "path": "ratio"},
        {"kind": "type_is", "type": "object"},
        {"kind": "keys_present", "keys": ["words", "top"]},
        {"kind": "keys_present", "keys": ["word"], "path": "top[0]"},
        {"kind": "numeric_close", "value": 0.5000001, "tolerance": 0.001, "path": "ratio"},
        {"kind": "numeric_close", "value": 3, "path": "words"},
        {"kind": "length_between", "min": 1, "max": 3, "path": "top"},
        {"kind": "length_between", "max": 5, "path": "name"},
        {"kind": "no_error"},
    ],
)
def test_each_kind_can_pass(raw: dict[str, Any]) -> None:
    assert passes(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "equals", "value": {"words": 4}},
        {"kind": "json_path_equals", "path": "top[1].count", "value": 2},
        {"kind": "json_path_equals", "path": "top[9].count", "value": 1},
        {"kind": "json_path_equals", "path": "nothing", "value": 1},
        {"kind": "type_is", "type": "integer", "path": "ratio"},
        {"kind": "type_is", "type": "integer", "path": "flag"},  # a boolean is not an integer
        {"kind": "keys_present", "keys": ["words", "absent"]},
        {"kind": "keys_present", "keys": ["x"], "path": "name"},  # not an object
        {"kind": "numeric_close", "value": 0.6, "path": "ratio"},
        {"kind": "numeric_close", "value": 1, "path": "flag"},  # a boolean is not a number
        {"kind": "numeric_close", "value": 1, "path": "name"},
        {"kind": "length_between", "min": 3, "path": "top"},
        {"kind": "length_between", "max": 0, "path": "name"},
        {"kind": "length_between", "max": 1, "path": "words"},  # an integer has no length
        {"kind": "tool_error"},
    ],
)
def test_each_kind_can_fail_with_a_reason(raw: dict[str, Any]) -> None:
    verdict = evaluate(parse_check(raw), good(RESULT))
    assert not verdict.ok and verdict.reason


def test_one_and_one_point_zero_are_told_apart_by_type_is_and_not_by_equals() -> None:
    assert passes({"kind": "type_is", "type": "integer"}, good(1))
    assert not passes({"kind": "type_is", "type": "integer"}, good(1.0))
    assert passes({"kind": "type_is", "type": "number"}, good(1.0))
    assert passes({"kind": "equals", "value": 1}, good(1.0))  # numbers compare by value, as in G3
    assert not passes({"kind": "equals", "value": 1}, good(True))  # but true is not 1
    assert not passes({"kind": "equals", "value": 0}, good(False))


def test_an_error_outcome_fails_every_check_but_tool_error_and_tool_error_needs_exactly_that() -> None:
    for raw in ({"kind": "no_error"}, {"kind": "equals", "value": 1}, {"kind": "type_is", "type": "null"}):
        verdict = evaluate(parse_check(raw), bad("ToolError", "empty text"))
        assert not verdict.ok and "ToolError" in verdict.reason
    assert passes({"kind": "tool_error"}, bad("ToolError"))
    for kind in ("Crash", "Timeout", "OutputTooLarge", "NotJson"):
        assert not passes({"kind": "tool_error"}, bad(kind))


def test_a_result_of_none_is_a_result() -> None:
    assert passes({"kind": "no_error"}, good(None))
    assert passes({"kind": "type_is", "type": "null"}, good(None))


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "python", "code": "__import__('os').system('id')"},
        {"kind": "equals", "value": 1, "code": "x"},  # an extra field
        {"kind": "equals"},  # no value
        {"kind": "type_is", "type": "float"},
        {"kind": "keys_present", "keys": []},
        {"kind": "keys_present", "keys": [1, 2]},
        {"kind": "numeric_close", "value": "3"},
        {"kind": "numeric_close", "value": float("nan")},
        {"kind": "numeric_close", "value": 1, "tolerance": -1},
        {"kind": "length_between"},
        {"kind": "length_between", "min": 3, "max": 1},
        {"kind": "length_between", "min": -1},
        {"kind": "json_path_equals", "path": "../x", "value": 1},
        {"kind": "json_path_equals", "path": "a..b", "value": 1},
        {"kind": "json_path_equals", "path": "a[-1]", "value": 1},
        {"kind": "json_path_equals", "path": ".a", "value": 1},
        {"kind": "json_path_equals", "path": "a" * 300, "value": 1},
        {"kind": "json_path_equals", "path": ".".join("a" for _ in range(20)), "value": 1},
        {"kind": "equals", "value": float("inf")},
        {"kind": "equals", "value": {1, 2}},
        {"kind": "equals", "value": "x" * 5000},
        {"kind": "no_error", "path": "a"},
        {"kind": 7},
        {},
        [],
        "equals",
        None,
    ],
)
def test_anything_outside_the_vocabulary_is_refused(raw: Any) -> None:
    with pytest.raises(ValueError):
        parse_check(raw)


def test_a_check_cannot_be_changed_after_parsing() -> None:
    check = parse_check({"kind": "equals", "value": 1})
    with pytest.raises(Exception):  # noqa: B017 (pydantic's frozen-instance error)
        check.value = 2  # type: ignore[misc]


def test_a_hostile_result_is_judged_without_error() -> None:
    deep: Any = 1
    for _ in range(3000):
        deep = [deep]
    verdict = evaluate(parse_check({"kind": "equals", "value": 1}), good(deep))
    assert not verdict.ok


def test_describe_says_what_was_expected_in_words() -> None:
    assert "equals" in describe(parse_check({"kind": "equals", "value": 3}))
    assert "top[0].word" in describe(
        parse_check({"kind": "json_path_equals", "path": "top[0].word", "value": "a"})
    )
    assert "type integer" in describe(parse_check({"kind": "type_is", "type": "integer"}))
    assert "ToolError" in describe(parse_check({"kind": "tool_error"}))
    for kind in CHECK_KINDS:
        sample = {
            "equals": {"value": 1},
            "json_path_equals": {"path": "a", "value": 1},
            "type_is": {"type": "string"},
            "keys_present": {"keys": ["a"]},
            "numeric_close": {"value": 1},
            "length_between": {"min": 1},
            "no_error": {},
            "tool_error": {},
        }[kind]
        assert describe(parse_check({"kind": kind, **sample}))


def test_json_type_names() -> None:
    assert [checks_dsl.json_type(v) for v in (None, True, 1, 1.5, "s", [], {})] == [
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "array",
        "object",
    ]
