"""A hostile schema must not hang or exhaust the host (`toolforge.schemacheck`): regex and reference bombs are cut off."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from typing import Any

import pytest

from toolforge import schemacheck
from toolforge.config import Settings
from toolforge.models import package as package_module
from toolforge.models.package import ToolPackage
from toolforge.schemacheck import SchemaCheckError, check_instances

Payload = dict[str, Any]


@pytest.fixture
def quick_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(package_module, "SCHEMA_CHECK_TIMEOUT_S", 1.0)


def _regex_bomb(payload: Payload) -> None:
    payload["manifest"]["input_schema"]["properties"]["text"] = {"type": "string", "pattern": "^(a+)+$"}
    payload["manifest"]["examples"][0]["input"] = {"text": "a" * 40 + "!"}


def _reference_bomb(payload: Payload) -> None:
    """Each definition refers to the one below it twice, so checking one value takes 2**40 steps."""
    defs: dict[str, Any] = {"d0": {"type": "string"}}
    for level in range(1, 41):
        defs[f"d{level}"] = {"allOf": [{"$ref": f"#/$defs/d{level - 1}"}, {"$ref": f"#/$defs/d{level - 1}"}]}
    schema = payload["manifest"]["input_schema"]
    schema["$defs"] = defs
    schema["properties"]["text"] = {"$ref": "#/$defs/d40"}


@pytest.mark.parametrize("bomb", [_regex_bomb, _reference_bomb], ids=["regex", "references"])
def test_a_schema_bomb_is_cut_off_and_reported(
    bomb: Callable[[Payload], None],
    make_payload: Callable[[], Payload],
    settings: Settings,
    quick_timeout: None,
) -> None:
    payload = make_payload()
    bomb(payload)
    started = time.monotonic()
    report = ToolPackage.from_submission(payload).validate(settings)
    assert time.monotonic() - started < 8
    assert [issue.code for issue in report.issues] == ["schema.too_expensive"]


def test_the_timeout_kills_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class Recording(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            spawned.append(self)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    bomb = {"type": "string", "pattern": "^(a+)+$"}
    with pytest.raises(SchemaCheckError):
        check_instances([(bomb, ["a" * 40 + "!"])], timeout_s=1.0)
    assert spawned and all(child.poll() is not None for child in spawned), (
        "the child process was left running"
    )


def test_isolated_and_inline_checks_agree() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}, "required": ["n"]}
    instances = [{"n": 1}, {"n": -1}, {}, {"n": "x"}]
    isolated = check_instances([(schema, instances)])
    inline = check_instances([(schema, instances)], isolated=False)
    assert isolated == inline
    assert [r is None for r in isolated[0]] == [True, False, False, False]


def test_reasons_are_short_and_printable() -> None:
    schema = {"type": "string", "pattern": "^x$"}
    [[reason]] = check_instances([(schema, ["a\x07" * 500])])
    assert reason is not None and len(reason) <= 260 and reason.isprintable()


def test_an_empty_request_does_not_start_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a process was started"))
    assert check_instances([]) == []


def test_the_child_gets_no_toolforge_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_LLM_API_KEY", "not-for-the-child")
    monkeypatch.setenv("HARMLESS_VARIABLE", "1")
    seen: dict[str, str] = {}
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs["env"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(schemacheck.subprocess, "run", spy)
    check_instances([({"type": "string"}, ["x"])])
    assert "TOOLFORGE_LLM_API_KEY" not in seen and seen.get("HARMLESS_VARIABLE") == "1"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, ""), (0, "not json"), (0, "[]"), (0, '[["a", "b"]]'), (0, '"text"')],
    ids=["crash", "garbage", "wrong_length", "wrong_row_length", "wrong_type"],
)
def test_a_misbehaving_child_is_an_error_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    monkeypatch.setattr(
        schemacheck.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout=stdout, stderr=""),
    )
    with pytest.raises(SchemaCheckError):
        check_instances([({"type": "string"}, ["x"])])
