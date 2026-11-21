"""`verify.schemagen`: every generated input validates, the same seed gives the same list, and the edges of each type are covered."""

from __future__ import annotations

import json
from typing import Any

import pytest

from toolforge import schemacheck
from toolforge.verify import schemagen
from toolforge.verify.schemagen import GeneratedInput, generate

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "maxLength": 50},
        "n": {"type": "integer", "minimum": 1, "maximum": 10},
        "ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "mode": {"enum": ["a", "b", "c"]},
        "flag": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "nested": {
            "type": "object",
            "properties": {"inner": {"type": "string"}},
            "required": ["inner"],
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}
EXAMPLES = [{"input": {"text": "hi", "n": 3, "mode": "a"}, "files": {"a.txt": "data"}, "live": False}]


def validates(schema: dict[str, Any], args: dict[str, Any]) -> bool:
    return schemacheck.check_instances([(schema, [args])])[0][0] is None


def test_every_generated_input_validates() -> None:
    inputs = generate(SCHEMA, EXAMPLES, seed="s", count=60)
    assert len(inputs) > 20
    assert all(validates(SCHEMA, item.args) for item in inputs)


def test_the_same_seed_gives_the_same_list_and_another_seed_a_different_order() -> None:
    first = generate(SCHEMA, EXAMPLES, seed="one", count=25)
    assert first == generate(SCHEMA, EXAMPLES, seed="one", count=25)
    assert first != generate(SCHEMA, EXAMPLES, seed="two", count=25)


def test_inputs_are_distinct_and_bounded_by_count() -> None:
    inputs = generate(SCHEMA, EXAMPLES, seed="s", count=12)
    assert len(inputs) <= 12
    keys = {json.dumps([i.args, i.files], sort_keys=True) for i in inputs}
    assert len(keys) == len(inputs)
    assert generate(SCHEMA, EXAMPLES, seed="s", count=0) == []


def test_the_two_structural_variants_come_first() -> None:
    inputs = generate(SCHEMA, EXAMPLES, seed="s")
    assert inputs[0].args == {"text": "hello world"}
    assert set(inputs[1].args) == set(SCHEMA["properties"])


def _values(name: str, inputs: list[GeneratedInput]) -> list[Any]:
    return [i.args[name] for i in inputs if name in i.args]


def test_string_edges_are_covered() -> None:
    values = _values("text", generate(SCHEMA, EXAMPLES, seed="s", count=200))
    assert "" in values
    assert any(isinstance(v, str) and len(v) == 50 for v in values)  # the longest allowed
    assert any(isinstance(v, str) and not v.isascii() for v in values)
    assert all(len(v) <= 50 for v in values)


def test_integer_edges_are_covered_and_stay_inside_the_range() -> None:
    values = _values("n", generate(SCHEMA, EXAMPLES, seed="s", count=200))
    assert {1, 10} <= set(values)
    assert all(1 <= v <= 10 for v in values)


def test_number_edges_are_covered() -> None:
    values = _values("ratio", generate(SCHEMA, EXAMPLES, seed="s", count=200))
    assert 0 in values
    assert 1 in values
    assert all(0 <= v <= 1 for v in values)


def test_every_enum_member_and_both_booleans_appear() -> None:
    inputs = generate(SCHEMA, EXAMPLES, seed="s", count=200)
    assert set(_values("mode", inputs)) == {"a", "b", "c"}
    assert set(_values("flag", inputs)) == {True, False}


def test_arrays_cover_empty_and_full() -> None:
    sizes = {len(v) for v in _values("tags", generate(SCHEMA, EXAMPLES, seed="s", count=200))}
    assert {0, 3} <= sizes


def test_examples_are_changed_one_property_at_a_time_and_keep_their_files() -> None:
    inputs = [i for i in generate(SCHEMA, EXAMPLES, seed="s", count=200) if i.label.startswith("example 1")]
    assert inputs
    assert all(i.files == {"a.txt": "data"} for i in inputs)
    base = EXAMPLES[0]["input"]
    for item in inputs:
        changed = {
            k for k in set(item.args) | set(base) if item.args.get(k, object()) != base.get(k, object())
        }
        assert len(changed) == 1, item.label
    assert any(i.label == "example 1 without n" for i in inputs)  # optional property left out
    assert not any(i.label == "example 1 without text" for i in inputs)  # required one never is


def test_live_examples_are_not_mutated() -> None:
    live = [{"input": {"text": "hi"}, "files": {}, "live": True}]
    assert not [i for i in generate(SCHEMA, live, seed="s", count=200) if i.label.startswith("example")]


def test_example_models_work_as_well_as_dicts() -> None:
    from toolforge.models.package import Example

    inputs = generate(SCHEMA, [Example(input={"text": "hi"})], seed="s", count=200)
    assert any(i.label.startswith("example 1") for i in inputs)


def test_a_candidate_the_validator_rejects_is_dropped() -> None:
    inputs = generate(SCHEMA, EXAMPLES, seed="s", count=200, validate=lambda cs: ["n" not in c for c in cs])
    assert inputs
    assert all("n" not in i.args for i in inputs)


def test_references_const_and_combiners() -> None:
    schema = {
        "type": "object",
        "properties": {
            "kind": {"const": "fixed"},
            "item": {"$ref": "#/$defs/item"},
            "either": {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 5}]},
        },
        "required": ["kind", "item"],
        "$defs": {"item": {"type": "string", "minLength": 3, "maxLength": 6}},
    }
    inputs = generate(schema, (), seed=1, count=100)
    assert inputs
    assert all(validates(schema, i.args) for i in inputs)
    assert {i.args["kind"] for i in inputs} == {"fixed"}
    assert all(3 <= len(i.args["item"]) <= 6 for i in inputs)


def test_an_impossible_schema_yields_nothing_instead_of_failing() -> None:
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 5, "maximum": 1}},
        "required": ["n"],
    }
    assert generate(schema, (), seed="s") == []


def test_a_recursive_schema_terminates() -> None:
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#"}, "name": {"type": "string"}},
        "required": ["name"],
    }
    inputs = generate(schema, (), seed="s", count=20)
    assert inputs
    assert all(validates(schema, i.args) for i in inputs)


def test_non_object_schemas_do_not_crash() -> None:
    # arguments are always an object in a package, so a non-object schema yields no inputs
    assert generate({"type": "string"}, (), seed="s") == []
    assert generate({}, (), seed="s")


def test_describe_value_never_returns_a_long_text() -> None:
    assert len(schemagen.describe_value("x" * 10_000)) < 60
    assert schemagen.describe_value("") == "an empty string"
    assert schemagen.describe_value([1, 2]) == "a list of 2"


def test_labels_never_hold_a_long_text() -> None:
    assert all(len(i.label) < 120 for i in generate(SCHEMA, EXAMPLES, seed="s", count=200))


@pytest.mark.parametrize("seed", ["a", "b", 7])
def test_validity_holds_for_every_seed(seed: str | int) -> None:
    assert all(validates(SCHEMA, i.args) for i in generate(SCHEMA, EXAMPLES, seed=seed))
