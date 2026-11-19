"""`validate` never raises on bad content, whatever shape it takes, and `from_submission` accepts any payload."""

from __future__ import annotations

import copy
import random
import time
from collections.abc import Callable
from typing import Any

import pytest

from toolforge.config import Settings
from toolforge.errors import ValidationError
from toolforge.models.package import ToolPackage

pytestmark = pytest.mark.usefixtures("inline_schema_check")
Payload = dict[str, Any]


def _validate(payload: Any, settings: Settings) -> Any:
    return ToolPackage.from_submission(payload).validate(settings)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        5,
        "text",
        [],
        [1, 2],
        {},
        {"manifest": None},
        {"files": None},
        {"manifest": {}, "files": []},
        b"bytes",
    ],
    ids=[
        "none",
        "int",
        "str",
        "list",
        "list2",
        "empty",
        "manifest_none",
        "files_none",
        "files_list",
        "bytes",
    ],
)
def test_wrong_shaped_submissions_become_issues(payload: Any, settings: Settings) -> None:
    report = _validate(payload, settings)
    assert not report.ok and all(issue.code and issue.hint for issue in report.issues)


def test_unknown_top_level_keys_are_reported(make_payload: Callable[[], Payload], settings: Settings) -> None:
    payload = make_payload()
    payload["extra"] = 1
    assert {i.code for i in _validate(payload, settings).issues} == {"field.unknown"}


def test_files_with_non_text_keys_and_values(make_payload: Callable[[], Payload], settings: Settings) -> None:
    payload = make_payload()
    payload["files"].update({1: "x", None: "x", "ok.txt": b"bytes", "n.txt": None})
    codes = {i.code for i in _validate(payload, settings).issues}
    assert codes == {"file.path", "file.type"}


def test_a_hundred_thousand_files_is_cheap(make_payload: Callable[[], Payload], settings: Settings) -> None:
    payload = make_payload()
    payload["files"].update({f"f{i}.txt": "x" for i in range(100_000)})
    started = time.monotonic()
    report = _validate(payload, settings)
    assert time.monotonic() - started < 2
    assert {i.code for i in report.issues} == {"files.too_many"}


def test_extreme_nesting_in_the_manifest_does_not_overflow_the_stack(
    make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    deep: Any = []
    for _ in range(100_000):
        deep = [deep]
    payload["manifest"]["examples"][0]["output"] = deep
    assert {i.code for i in _validate(payload, settings).issues} == {"manifest.too_deep"}


def test_a_huge_string_value_is_bounded(make_payload: Callable[[], Payload], settings: Settings) -> None:
    payload = make_payload()
    payload["manifest"]["description"] = "x" * 5_000_000
    report = _validate(payload, settings)
    assert report.issues and all(len(issue.message) < 400 for issue in report.issues)


def test_python_that_is_hard_to_parse_is_an_issue(
    make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    payload["files"]["helper.py"] = "x = " + "(" * 5000 + "1" + ")" * 5000 + "\n"
    payload["files"]["null.py"] = "x = 1\x00\n"
    codes = {i.code for i in _validate(payload, settings).issues}
    assert codes <= {"file.syntax", "file.nul"} and codes


def test_messages_never_echo_long_or_unprintable_untrusted_text(
    make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    payload["manifest"]["name"] = "bad\x1b[31m" + "X" * 5000
    payload["manifest"]["permissions"]["network"] = ["\x07" + "h" * 5000]
    payload["files"]["we\x07ird/" + "p" * 300 + ".exe"] = "x"
    report = _validate(payload, settings)
    assert report.issues
    for issue in report.issues:
        assert len(issue.message) < 400 and len(issue.path) < 400
        assert issue.message.isprintable() and issue.path.isprintable(), repr(issue)


def _random_value(rng: random.Random, depth: int = 0) -> Any:
    choice = rng.randrange(9 if depth < 3 else 6)
    if choice == 0:
        return None
    if choice == 1:
        return rng.choice([True, False])
    if choice == 2:
        return rng.choice([0, 1, -1, 2**70, 10**400, 3.5, float("inf"), float("nan")])
    if choice == 3:
        return rng.choice(["", "a", "tool.py:run", "1.0.0", "\x00", "\ud800", "x" * 700, "*.com", "../x"])
    if choice == 4:
        return rng.choice([b"x", (1, 2), {1, 2}, object()])
    if choice == 5:
        return rng.choice([[], {}, ""])
    if choice == 6:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    keys = [
        "name",
        "version",
        "description",
        "entrypoint",
        "kind",
        "input_schema",
        "examples",
        "limits",
        "x",
        1,
        None,
    ]
    return {rng.choice(keys): _random_value(rng, depth + 1) for _ in range(rng.randrange(5))}


def test_random_garbage_never_raises(make_payload: Callable[[], Payload], settings: Settings) -> None:
    rng = random.Random(20261119)
    for _ in range(400):
        payload = make_payload()
        manifest = payload["manifest"]
        targets = [manifest, manifest["permissions"], manifest["limits"], manifest["examples"][0]]
        for _ in range(rng.randrange(1, 4)):
            target = rng.choice(targets)
            key = (
                rng.choice([*target.keys(), "extra"])
                if rng.random() < 0.8
                else rng.choice(["name", "version"])
            )
            target[key] = _random_value(rng)
        if rng.random() < 0.3:
            payload["files"][rng.choice(["tool.py", "tests/test_tool.py", "x.py", 3, "../a"])] = (
                _random_value(rng)
            )
        if rng.random() < 0.1:
            payload = _random_value(rng)
        report = _validate(copy.deepcopy(payload) if isinstance(payload, dict) else payload, settings)
        assert report.ok or report.issues
        assert "package.internal_error" not in {i.code for i in report.issues}, payload


def test_manifest_property_refuses_an_invalid_manifest(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    payload["manifest"]["version"] = "nope"
    with pytest.raises(ValidationError):
        _ = ToolPackage.from_submission(payload).manifest


def test_manifest_property_returns_the_parsed_model(make_payload: Callable[[], Payload]) -> None:
    manifest = ToolPackage.from_submission(make_payload()).manifest
    assert (
        manifest.name == "word_count"
        and manifest.limits.timeout_s == 10
        and manifest.examples[0].live is False
    )


def test_the_submitted_payload_is_never_mutated(
    make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    before = copy.deepcopy(payload)
    package = ToolPackage.from_submission(payload)
    package.validate(settings)
    package.to_tar()
    assert payload == before


def test_a_late_change_to_the_payload_does_not_change_the_package(
    make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    package = ToolPackage.from_submission(payload)
    payload["manifest"]["name"] = "Changed Afterwards"
    payload["files"]["tool.py"] = "evil"
    assert package.validate(settings).ok and package.manifest.name == "word_count"
