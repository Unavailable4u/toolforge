"""One test per validation rule: a valid base package, mutated to break exactly that rule, asserting on the stable `code`.

`test_every_catalogued_code_has_a_case` fails when a code is added to `ISSUE_CODES` without a case here, so the catalogue,
the rules and these tests cannot drift apart.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from plan_helpers import plan_payload

from toolforge.config import Settings
from toolforge.models import package as package_module
from toolforge.models.package import ISSUE_CODES, ToolPackage

pytestmark = pytest.mark.usefixtures("inline_schema_check")

Payload = dict[str, Any]
Mutation = Callable[[Payload], None]


def _m(payload: Payload) -> dict[str, Any]:
    return payload["manifest"]


def _deep(levels: int) -> list:
    value: list = []
    for _ in range(levels):
        value = [value]
    return value


def _nest_schema(levels: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(levels):
        schema = {"type": "array", "items": schema}
    return schema


def _set(key: str, value: Any) -> Mutation:
    return lambda p: _m(p).update({key: value})


def _drop(key: str) -> Mutation:
    return lambda p: _m(p).pop(key)


def _permission(key: str, value: Any) -> Mutation:
    return lambda p: _m(p)["permissions"].update({key: value})


def _limit(key: str, value: Any) -> Mutation:
    return lambda p: _m(p)["limits"].update({key: value})


def _file(path: str, text: Any) -> Mutation:
    return lambda p: p["files"].update({path: text})


def _plan(*changes: Mutation) -> Mutation:
    """Replace the base package with a valid browser plan, then apply `changes` to that."""

    def mutate(payload: Payload) -> None:
        payload.clear()
        payload.update(plan_payload())
        for change in changes:
            change(payload)

    return mutate


def _example(**changes: Any) -> Mutation:
    return lambda p: _m(p)["examples"][0].update(changes)


def _many_files(p: Payload) -> None:
    p["files"].update({f"data/f{i}.txt": "x" for i in range(41)})


def _many_properties(p: Payload) -> None:
    _m(p)["input_schema"]["properties"] = {f"p{i}": {"type": "string"} for i in range(41)}


# (case id, expected code, mutation). Each mutation breaks exactly the rule its code names.
ERROR_CASES: list[tuple[str, str, Mutation]] = [
    ("manifest_missing", "manifest.missing", lambda p: p.pop("manifest")),
    ("manifest_not_object", "manifest.not_object", lambda p: p.update({"manifest": [1, 2]})),
    ("manifest_too_deep", "manifest.too_deep", _example(output=_deep(60))),
    ("manifest_nan", "manifest.not_json", _example(output=float("nan"))),
    ("manifest_huge_int", "manifest.not_json", _example(output=10**400)),
    ("manifest_surrogate", "manifest.not_json", _set("description", "x" * 30 + "\ud800")),
    ("field_required", "field.required", _drop("version")),
    ("field_unknown", "field.unknown", _set("surprise", 1)),
    ("field_type", "field.type", _set("name", 5)),
    ("name_uppercase", "name.invalid", _set("name", "Word_count")),
    ("name_too_short", "name.invalid", _set("name", "ab")),
    ("name_too_long", "name.invalid", _set("name", "a" * 42)),
    ("name_digit_first", "name.invalid", _set("name", "1word")),
    ("name_forge_prefix", "name.reserved_prefix", _set("name", "forge_tool")),
    ("name_tf_prefix", "name.reserved_prefix", _set("name", "tf_tool")),
    ("version_two_parts", "version.invalid", _set("version", "1.0")),
    ("version_leading_zero", "version.invalid", _set("version", "01.0.0")),
    ("version_prerelease", "version.invalid", _set("version", "1.0.0-beta")),
    ("parent_invalid", "parent_version.invalid", _set("parent_version", "one")),
    ("parent_equal", "parent_version.not_lower", _set("parent_version", "1.0.0")),
    ("parent_higher", "parent_version.not_lower", _set("parent_version", "2.0.0")),
    ("description_short", "description.length", _set("description", "too short")),
    ("description_long", "description.length", _set("description", "x" * 601)),
    ("description_blank_padded", "description.length", _set("description", "   short   " + " " * 40)),
    ("description_control", "description.invalid_chars", _set("description", "x" * 30 + "\x07")),
    ("description_newline", "description.invalid_chars", _set("description", "x" * 30 + "\nmore")),
    ("description_zero_width", "description.invalid_chars", _set("description", "x" * 30 + "\u200b")),
    ("when_to_use_long", "when_to_use.length", _set("when_to_use", "x" * 301)),
    ("when_to_use_control", "when_to_use.invalid_chars", _set("when_to_use", "use it\x00 when")),
    ("entrypoint_no_colon", "entrypoint.format", _set("entrypoint", "tool.py")),
    ("entrypoint_not_py", "entrypoint.format", _set("entrypoint", "tool.txt:run")),
    ("entrypoint_traversal", "entrypoint.format", _set("entrypoint", "../tool.py:run")),
    ("entrypoint_bad_function", "entrypoint.format", _set("entrypoint", "tool.py:not-valid")),
    ("entrypoint_keyword", "entrypoint.format", _set("entrypoint", "tool.py:class")),
    ("entrypoint_missing_file", "entrypoint.missing_file", _set("entrypoint", "other.py:run")),
    ("entrypoint_missing_function", "entrypoint.missing_function", _set("entrypoint", "tool.py:absent")),
    (
        "entrypoint_in_class",
        "entrypoint.missing_function",
        _file("tool.py", "class T:\n    def run(self, args):\n        return {}\n"),
    ),
    ("entrypoint_assigned", "entrypoint.missing_function", _file("tool.py", "run = lambda args: {}\n")),
    (
        "entrypoint_async",
        "entrypoint.missing_function",
        _file("tool.py", "async def run(args):\n    return {}\n"),
    ),
    (
        "entrypoint_in_if",
        "entrypoint.missing_function",
        _file("tool.py", "if True:\n    def run(args):\n        return {}\n"),
    ),
    ("kind_unknown", "kind.invalid", _set("kind", "daemon")),
    ("schema_invalid_type", "schema.invalid", lambda p: _m(p)["input_schema"].update({"type": "bogus"})),
    (
        "schema_bad_regex",
        "schema.invalid",
        lambda p: _m(p)["input_schema"]["properties"].update({"x": {"type": "string", "pattern": "(["}}),
    ),
    (
        "schema_dangling_ref",
        "schema.invalid",
        lambda p: _m(p)["input_schema"]["properties"].update({"x": {"$ref": "#/$defs/nope"}}),
    ),
    (
        "schema_wrong_draft",
        "schema.invalid",
        lambda p: _m(p)["input_schema"].update({"$schema": "http://json-schema.org/draft-04/schema#"}),
    ),
    (
        "output_schema_invalid",
        "schema.invalid",
        lambda p: _m(p).update({"output_schema": {"type": "object", "properties": 5}}),
    ),
    ("schema_not_object", "schema.not_object", lambda p: _m(p)["input_schema"].update({"type": "array"})),
    (
        "schema_open_properties",
        "schema.additional_properties",
        lambda p: _m(p)["input_schema"].pop("additionalProperties"),
    ),
    (
        "schema_additional_true",
        "schema.additional_properties",
        lambda p: _m(p)["input_schema"].update({"additionalProperties": True}),
    ),
    (
        "schema_remote_ref",
        "schema.remote_ref",
        lambda p: _m(p)["input_schema"]["properties"].update({"x": {"$ref": "https://evil.example/s.json"}}),
    ),
    (
        "schema_file_ref",
        "schema.remote_ref",
        lambda p: _m(p)["input_schema"]["properties"].update({"x": {"$ref": "file:///etc/passwd"}}),
    ),
    (
        "schema_too_deep",
        "schema.too_deep",
        lambda p: _m(p)["input_schema"]["properties"].update({"x": _nest_schema(7)}),
    ),
    ("schema_too_many_properties", "schema.too_many_properties", _many_properties),
    (
        "network_too_many",
        "permissions.network.too_many",
        _permission("network", [f"h{i}.example.com" for i in range(9)]),
    ),
    ("network_scheme", "permissions.network.invalid_host", _permission("network", ["https://a.example.com"])),
    ("network_port", "permissions.network.invalid_host", _permission("network", ["a.example.com:443"])),
    ("network_wildcard", "permissions.network.invalid_host", _permission("network", ["*.example.com"])),
    ("network_uppercase", "permissions.network.invalid_host", _permission("network", ["API.example.com"])),
    ("network_ipv4", "permissions.network.invalid_host", _permission("network", ["10.0.0.1"])),
    ("network_ipv6", "permissions.network.invalid_host", _permission("network", ["[::1]"])),
    ("network_path", "permissions.network.invalid_host", _permission("network", ["a.example.com/x"])),
    ("network_empty", "permissions.network.invalid_host", _permission("network", [""])),
    ("secrets_too_many", "permissions.secrets.too_many", _permission("secrets", [f"s{i}" for i in range(5)])),
    ("secrets_uppercase", "permissions.secrets.invalid_name", _permission("secrets", ["API_KEY"])),
    ("secrets_too_long", "permissions.secrets.invalid_name", _permission("secrets", ["a" * 25])),
    ("browser_on_a_stateless_tool", "permissions.browser.kind", _permission("browser", True)),
    ("plan_without_browser", "permissions.browser.kind", _plan(_permission("browser", False))),
    ("plan_entrypoint_python", "plan.entrypoint", _plan(_set("entrypoint", "tool.py:run"))),
    ("plan_with_secret", "plan.permissions", _plan(_permission("secrets", ["github"]))),
    ("plan_with_inputs", "plan.permissions", _plan(_permission("inputs", True))),
    (
        "plan_example_with_files",
        "plan.permissions",
        _plan(lambda p: _m(p)["examples"][0].update({"files": {"inputs/a.txt": "a"}})),
    ),
    ("plan_requirements", "plan.requirements", _plan(_set("requirements", ["requests==2.0.0"]))),
    ("plan_with_code", "plan.files", _plan(_file("tool.py", "def run(args):\n    return 1\n"))),
    ("plan_with_tests", "plan.files", _plan(_file("tests/test_tool.py", "def test_a():\n    assert 1\n"))),
    ("plan_stray_data", "plan.files", _plan(_file("data.json", "{}"))),
    ("plan_missing", "plan.missing", _plan(lambda p: p["files"].pop("plan.json"))),
    ("plan_not_json", "plan.invalid", _plan(_file("plan.json", "{"))),
    ("plan_unknown_step", "plan.invalid", _plan(_file("plan.json", '{"steps": [{"step": "evaluate"}]}'))),
    (
        "plan_other_host",
        "plan.invalid",
        _plan(_permission("network", ["elsewhere.example.org"])),
    ),
    ("timeout_zero", "limits.timeout_s.range", _limit("timeout_s", 0)),
    ("timeout_high", "limits.timeout_s.range", _limit("timeout_s", 121)),
    ("memory_low", "limits.memory_mb.range", _limit("memory_mb", 63)),
    ("memory_high", "limits.memory_mb.range", _limit("memory_mb", 513)),
    ("output_zero", "limits.output_kb.range", _limit("output_kb", 0)),
    ("output_high", "limits.output_kb.range", _limit("output_kb", 257)),
    (
        "requirements_too_many",
        "requirements.too_many",
        _set("requirements", [f"pkg{i}==1.0" for i in range(11)]),
    ),
    ("requirements_range", "requirements.invalid", _set("requirements", ["requests>=2"])),
    ("requirements_no_version", "requirements.invalid", _set("requirements", ["requests"])),
    ("requirements_url", "requirements.invalid", _set("requirements", ["https://x.example/p.whl"])),
    ("requirements_vcs", "requirements.invalid", _set("requirements", ["git+https://x.example/p.git"])),
    ("requirements_at_url", "requirements.invalid", _set("requirements", ["pkg @ https://x.example/p.zip"])),
    ("requirements_path", "requirements.invalid", _set("requirements", ["./local/pkg"])),
    ("requirements_extras", "requirements.invalid", _set("requirements", ["requests[socks]==2.0.0"])),
    ("requirements_marker", "requirements.invalid", _set("requirements", ["requests==2.0.0; os_name=='nt'"])),
    ("requirements_option", "requirements.invalid", _set("requirements", ["--index-url=https://x.example"])),
    ("requirements_local_tag", "requirements.invalid", _set("requirements", ["numpy==1.0+cpu"])),
    ("requirements_duplicate", "requirements.invalid", _set("requirements", ["Pkg-A==1.0", "pkg_a==2.0"])),
    ("examples_none", "examples.count", _set("examples", [])),
    (
        "examples_six",
        "examples.count",
        _set("examples", [{"input": {"text": "a"}, "output": {"words": 1}} for _ in range(6)]),
    ),
    ("example_input_wrong_type", "examples.input_invalid", _example(input={"text": 5})),
    ("example_input_extra_key", "examples.input_invalid", _example(input={"text": "a", "extra": 1})),
    ("example_input_missing_key", "examples.input_invalid", _example(input={})),
    ("example_file_outside_inputs", "examples.file_invalid", _example(files={"data.csv": "a"})),
    ("example_file_traversal", "examples.file_invalid", _example(files={"inputs/../x": "a"})),
    ("example_file_nul", "examples.file_invalid", _example(files={"inputs/a.txt": "a\x00"})),
    ("example_file_large", "examples.file_invalid", _example(files={"inputs/a.txt": "x" * 70000})),
    ("example_output_wrong_type", "examples.output_invalid", _example(output={"words": "two"})),
    ("example_output_missing_key", "examples.output_invalid", _example(output={})),
    ("files_too_many", "files.too_many", _many_files),
    ("file_traversal", "file.path", _file("../escape.py", "x = 1\n")),
    ("file_absolute", "file.path", _file("/etc/x.py", "x = 1\n")),
    ("file_backslash", "file.path", _file("pkg\\x.py", "x = 1\n")),
    ("file_hidden_segment", "file.path", _file(".hidden/x.py", "x = 1\n")),
    ("file_dot_segment", "file.path", _file("a/./x.py", "x = 1\n")),
    ("file_double_slash", "file.path", _file("a//x.py", "x = 1\n")),
    ("file_control_char", "file.path", _file("a\x01.py", "x = 1\n")),
    ("file_is_manifest", "file.path", _file("manifest.json", "{}")),
    ("file_extension", "file.extension", _file("run.sh", "echo hi\n")),
    ("file_no_extension", "file.extension", _file("Makefile", "all:\n")),
    ("file_too_large", "file.too_large", _file("data/big.txt", "x" * (64 * 1024 + 1))),
    ("file_lone_surrogate", "file.encoding", _file("data/a.txt", "a\ud800b")),
    ("file_nul", "file.nul", _file("data/a.txt", "a\x00b")),
    ("file_not_text", "file.type", _file("data/a.txt", 5)),
    ("file_syntax_error", "file.syntax", _file("helper.py", "def broken(:\n")),
    (
        "tests_too_few",
        "tests.too_few",
        _file("tests/test_tool.py", "def test_one():\n    assert 1\n\ndef test_two():\n    assert 2\n"),
    ),
    (
        "tests_not_top_level",
        "tests.too_few",
        _file(
            "tests/test_tool.py",
            "class T:\n    def test_a(self): pass\n    def test_b(self): pass\n    def test_c(self): pass\n",
        ),
    ),
    (
        "tests_wrong_folder",
        "tests.too_few",
        lambda p: p["files"].update({"test_tool.py": p["files"].pop("tests/test_tool.py")}),
    ),
]


def _validate(payload: Payload, settings: Settings) -> package_module.ValidationReport:
    return ToolPackage.from_submission(payload).validate(settings)


def test_base_package_is_valid(make_payload: Callable[[], Payload], settings: Settings) -> None:
    report = _validate(make_payload(), settings)
    assert report.ok, report.issues
    assert report.warnings == []


@pytest.mark.parametrize(("case_id", "code", "mutate"), ERROR_CASES, ids=[case[0] for case in ERROR_CASES])
def test_breaking_one_rule_reports_its_code(
    case_id: str, code: str, mutate: Mutation, make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    mutate(payload)
    report = _validate(payload, settings)
    assert not report.ok
    assert {issue.code for issue in report.issues} == {code}, report.issues
    for issue in report.issues:
        assert issue.path and issue.message and issue.hint, "every issue says where, what and what to change"
    assert not (ISSUE_CODES[code].severity != "error")


def test_a_plan_without_hosts_is_told_both_things(settings: Settings) -> None:
    payload = plan_payload(permissions={"network": [], "browser": True})
    report = _validate(payload, settings)
    assert {issue.code for issue in report.issues} == {"plan.permissions", "plan.invalid"}


def test_package_too_large_uses_the_setting(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    payload["files"]["data/a.txt"] = "x" * 60000
    payload["files"]["data/b.txt"] = "x" * 60000
    small = _validate(copy.deepcopy(payload), Settings(max_package_kb=64))
    assert {issue.code for issue in small.issues} == {"package.too_large"}
    assert _validate(payload, Settings(max_package_kb=256)).ok


def test_manifest_alone_over_the_limit_is_reported(make_payload: Callable[[], Payload]) -> None:
    payload = make_payload()
    payload["manifest"]["examples"][0]["output"] = {"words": 2, "pad": "x" * 3000}
    report = _validate(payload, Settings(max_package_kb=1))
    assert "package.too_large" in {issue.code for issue in report.issues}


def test_internal_failure_rejects_instead_of_raising(
    make_payload: Callable[[], Payload], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("boom with a secret path /root/x")

    monkeypatch.setattr(ToolPackage, "_check_files", explode)
    report = _validate(make_payload(), settings)
    assert [issue.code for issue in report.issues] == ["package.internal_error"]
    assert "/root/x" not in report.issues[0].message


WARNING_CASES: list[tuple[str, str, Mutation]] = [
    ("when_to_use_empty", "when_to_use.empty", _drop("when_to_use")),
    ("output_schema_missing", "output_schema.missing", _drop("output_schema")),
    ("examples_without_output", "examples.no_output", lambda p: _m(p)["examples"][0].pop("output")),
]


@pytest.mark.parametrize(
    ("case_id", "code", "mutate"), WARNING_CASES, ids=[case[0] for case in WARNING_CASES]
)
def test_advice_is_a_warning_not_an_error(
    case_id: str, code: str, mutate: Mutation, make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    mutate(payload)
    report = _validate(payload, settings)
    assert report.ok, report.issues
    assert {warning.code for warning in report.warnings} == {code}


# Codes that need more than a one-line mutation of a submission; each is covered in the file named.
COVERED_ELSEWHERE = {
    "manifest.invalid_json": "tests/test_package_dir.py",
    "package.too_large": "this file (settings-driven tests above)",
    "package.internal_error": "this file (test_internal_failure_rejects_instead_of_raising)",
    "schema.too_expensive": "tests/test_package_isolation.py",
}


def test_every_catalogued_code_has_a_case() -> None:
    tested = {code for _, code, _ in ERROR_CASES} | {code for _, code, _ in WARNING_CASES}
    missing = set(ISSUE_CODES) - tested - set(COVERED_ELSEWHERE)
    assert not missing, f"issue codes with no test case: {sorted(missing)}"
    unknown = (tested | set(COVERED_ELSEWHERE)) - set(ISSUE_CODES)
    assert not unknown, f"test cases for codes that do not exist: {sorted(unknown)}"


def test_catalogue_severity_matches_the_lists() -> None:
    assert {code for _, code, _ in ERROR_CASES} <= {
        c for c, i in ISSUE_CODES.items() if i.severity == "error"
    }
    assert {code for _, code, _ in WARNING_CASES} == {
        c for c, i in ISSUE_CODES.items() if i.severity == "warning"
    }


def test_every_error_reports_all_its_problems_at_once(
    make_payload: Callable[[], Payload], settings: Settings
) -> None:
    payload = make_payload()
    _m(payload).update({"name": "Bad", "version": "1", "kind": "x"})
    payload["files"]["notes.exe"] = "x"
    codes = {issue.code for issue in _validate(payload, settings).issues}
    assert codes == {"name.invalid", "version.invalid", "kind.invalid", "file.extension"}


def test_issues_are_capped(make_payload: Callable[[], Payload], settings: Settings) -> None:
    payload = make_payload()
    payload["files"].update({f"bad{i}.exe": "x" for i in range(500)})
    report = _validate(payload, settings)
    assert len(report.issues) <= package_module.MAX_ISSUES
