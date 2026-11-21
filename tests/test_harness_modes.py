"""The harness's `--tests`, `--stub` and `--batch` modes, run for real, but only on our own trusted code.

Same rules as `tests/test_harness.py`: the code that runs here lives in `tests/fixtures/trusted_tools/suite` or is written by this
file, never a generated or submitted package. The test sources are strings, not files, so pytest and ruff do not collect or lint
them as part of this repository's own suite (one of them is deliberately not valid Python).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from toolforge.sandbox import harness
from toolforge.services.runner import parse_envelope

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")

TRUSTED = Path(__file__).parent / "fixtures" / "trusted_tools"
SDK = Path(__file__).resolve().parent.parent / "src" / "toolforge" / "sdk.py"
NONCE = "cd" * 16

SUITE = """\
import time
import unittest

from tool import helper, run


def test_doubles():
    assert run({"n": 2}) == {"n": 4}


def test_wrong_on_purpose():
    assert run({"n": 2}) == {"n": 5}


def test_raises_something_else():
    raise KeyError("boom")


def test_prints_and_passes():
    print("hello from a test")
    assert helper(1) == 2


def test_takes_a_fixture(tmp_path):
    assert tmp_path


def test_sleeps_forever():
    time.sleep(60)


def test_skipped():
    raise unittest.SkipTest("not today")


def not_a_test():
    raise AssertionError("never collected")


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.base = 10

    def test_method_passes(self):
        self.assertEqual(run({"n": 1}), {"n": 2})

    def test_method_fails(self):
        self.assertEqual(run({"n": 1}), {"n": 99})

    def test_method_errors(self):
        raise ValueError("bad")

    def test_method_skips(self):
        self.skipTest("later")
"""


def workspace_with(tmp_path: Path, tests: dict[str, str] | None, request: dict[str, Any]) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(TRUSTED / "suite", workspace / "pkg")
    for name, source in (tests or {}).items():
        target = workspace / "pkg" / "tests" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    tf = workspace / ".tf"
    tf.mkdir()
    (tf / "harness.py").write_text(harness.HARNESS_SOURCE, encoding="utf-8")
    shutil.copy(SDK, tf / "toolforge_sdk.py")
    body = {"entrypoint": "tool.py:run", "limits": {"timeout_s": 5, "output_kb": 16}, **request}
    (tf / "in.json").write_text(json.dumps(body), encoding="utf-8")
    return workspace


def run_mode(
    tmp_path: Path,
    flags: list[str],
    request: dict[str, Any],
    tests: dict[str, str] | None = None,
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    workspace = workspace_with(tmp_path, tests, request)
    proc = subprocess.run(
        [sys.executable, str(workspace / ".tf" / "harness.py"), NONCE, *flags],
        capture_output=True,
        timeout=timeout,
        env={"PATH": os.environ.get("PATH", "")},
        cwd=tmp_path,
        check=False,
    )
    envelope = parse_envelope(proc.stdout, NONCE)
    assert envelope is not None, proc.stderr.decode(errors="replace")
    assert proc.returncode == 0
    return {"ok": envelope.ok, "result": envelope.result, "error": envelope.error, "workspace": workspace}


def report_of(tmp_path: Path, *, stub: bool = False, tests: dict[str, str] | None = None, **request: Any):
    ran = run_mode(
        tmp_path,
        ["--tests", *(["--stub"] if stub else [])],
        {"test_timeout_s": 2, **request},
        {"test_tool.py": SUITE} if tests is None else tests,
    )
    assert ran["ok"], ran["error"]
    return ran["result"]


# --- --tests ---------------------------------------------------------------------------------------------------------------------


def test_the_report_counts_every_outcome(tmp_path: Path) -> None:
    report = report_of(tmp_path)
    assert report["stub"] is False and report["files"] == 1
    # collected: 7 functions (one takes a fixture) and 4 methods; `not_a_test` is not a test
    assert report["ran"] == 10
    assert report["passed"] == 3  # test_doubles, test_prints_and_passes, test_method_passes
    assert report["skipped"] == 2
    failed = {item["id"]: item["error"] for item in report["failed"]}
    assert set(failed) == {
        "tests/test_tool.py::test_wrong_on_purpose",
        "tests/test_tool.py::test_sleeps_forever",
        "tests/test_tool.py::CaseTests.test_method_fails",
    }
    assert report["failed_count"] == 3
    assert "timed out after 2 seconds" in failed["tests/test_tool.py::test_sleeps_forever"]
    errors = {item["id"]: item["error"] for item in report["errors"]}
    assert set(errors) == {
        "tests/test_tool.py::test_raises_something_else",
        "tests/test_tool.py::test_takes_a_fixture",
        "tests/test_tool.py::CaseTests.test_method_errors",
    }
    assert report["error_count"] == 3
    assert report["not_run"] == 0


def test_an_error_names_the_exception_and_the_line_in_the_tests(tmp_path: Path) -> None:
    errors = {item["id"]: item["error"] for item in report_of(tmp_path)["errors"]}
    message = errors["tests/test_tool.py::test_raises_something_else"]
    assert message.startswith("KeyError: ") and "tests/test_tool.py:" in message
    assert "fixtures and parametrize are not supported" in errors["tests/test_tool.py::test_takes_a_fixture"]


def test_each_test_runs_in_its_own_process_with_its_own_printing(tmp_path: Path) -> None:
    ran = run_mode(tmp_path, ["--tests"], {"test_timeout_s": 2}, {"test_tool.py": SUITE})
    assert b"hello from a test" not in json.dumps(ran["result"]).encode()  # captured, not mixed in


def test_one_test_cannot_see_what_another_imported_or_changed(tmp_path: Path) -> None:
    source = (
        "import tool\n\n"
        "def test_first():\n    tool.helper = None\n    assert tool.helper is None\n\n"
        "def test_second():\n    assert tool.helper(1) == 2\n"
    )
    report = report_of(tmp_path, tests={"test_x.py": source})
    assert report["passed"] == 2


def test_a_test_that_exits_the_process_is_an_error_not_a_hang(tmp_path: Path) -> None:
    source = "import os\n\ndef test_dies():\n    os._exit(7)\n\ndef test_fine():\n    assert True\n"
    report = report_of(tmp_path, tests={"test_x.py": source})
    assert report["passed"] == 1
    assert report["error_count"] == 1 and "the test process failed" in report["errors"][0]["error"]


def test_system_exit_in_a_test_is_an_error(tmp_path: Path) -> None:
    report = report_of(tmp_path, tests={"test_x.py": "def test_a():\n    raise SystemExit(1)\n"})
    assert report["error_count"] == 1 and report["errors"][0]["error"].startswith("SystemExit")


def test_files_that_cannot_be_read_are_reported_and_the_rest_still_run(tmp_path: Path) -> None:
    report = report_of(
        tmp_path,
        tests={
            "test_syntax.py": "def test_a(:\n    pass\n",
            "test_import.py": "import module_that_does_not_exist\n\ndef test_a():\n    assert True\n",
            "test_good.py": "def test_a():\n    assert True\n",
        },
    )
    assert report["files"] == 3
    assert report["passed"] == 1
    ids = {item["id"] for item in report["errors"]}
    assert "tests/test_syntax.py" in ids and "tests/test_import.py::test_a" in ids
    import_error = next(i for i in report["errors"] if i["id"] == "tests/test_import.py::test_a")
    assert "ModuleNotFoundError" in import_error["error"]


def test_no_tests_is_an_empty_report_not_a_failure(tmp_path: Path) -> None:
    report = report_of(tmp_path, tests={})
    assert report["ran"] == 0 and report["files"] == 0 and report["passed"] == 0


def test_only_test_files_in_tests_are_collected(tmp_path: Path) -> None:
    report = report_of(
        tmp_path,
        tests={
            "helper.py": "def test_not_collected():\n    raise AssertionError\n",
            "test_a.py": "def test_a():\n    assert True\n",
            "sub/test_b.py": "def test_b():\n    assert True\n",
        },
    )
    assert report["ran"] == 2 and report["passed"] == 2


def test_the_deadline_leaves_the_rest_unrun(tmp_path: Path) -> None:
    source = "import time\n\n" + "".join(f"def test_{n}():\n    time.sleep(0.6)\n\n" for n in range(10))
    report = report_of(tmp_path, tests={"test_slow.py": source}, deadline_s=2)
    assert report["not_run"] > 0 and report["ran"] + report["not_run"] == 10


def test_a_long_error_is_clipped_and_the_list_is_bounded(tmp_path: Path) -> None:
    source = "".join(f"def test_{n}():\n    assert False, 'x' * 5000\n\n" for n in range(30))
    report = report_of(tmp_path, tests={"test_many.py": source})
    assert report["failed_count"] == 30 and len(report["failed"]) == harness_limit()
    assert all(len(item["error"]) <= 300 for item in report["failed"])


def harness_limit() -> int:
    return 20


def test_the_tests_see_the_workspace_as_their_working_directory(tmp_path: Path) -> None:
    source = "import os\n\ndef test_cwd():\n    assert os.path.basename(os.getcwd()) == 'workspace'\n"
    assert report_of(tmp_path, tests={"test_cwd.py": source})["passed"] == 1


# --- --stub ----------------------------------------------------------------------------------------------------------------------


def test_with_the_stub_tests_that_check_the_returned_value_fail(tmp_path: Path) -> None:
    report = report_of(tmp_path, stub=True)
    assert report["stub"] is True
    passed = report["passed"]
    assert passed == 1  # only test_prints_and_passes: it never calls the entrypoint
    all_ids = {item["id"] for item in report["failed"] + report["errors"]}
    assert "tests/test_tool.py::test_doubles" in all_ids
    assert "tests/test_tool.py::CaseTests.test_method_passes" in all_ids


def test_tests_that_assert_nothing_about_the_result_all_pass_under_the_stub(tmp_path: Path) -> None:
    source = (
        "from tool import run\n\n"
        "def test_a():\n    run({'n': 1})\n    assert True\n\n"
        "def test_b():\n    assert 1 + 1 == 2\n"
    )
    report = report_of(tmp_path, stub=True, tests={"test_x.py": source})
    assert report["ran"] == 2 and report["passed"] == 2
    assert report["failed_count"] == 0 and report["error_count"] == 0


def test_the_stub_replaces_the_function_for_both_import_styles(tmp_path: Path) -> None:
    source = (
        "import tool\n\n"
        "def test_attribute():\n    assert tool.run({'n': 1}) is None\n\n"
        "def test_from_import():\n    from tool import run\n    assert run({'n': 1}) is None\n"
    )
    assert report_of(tmp_path, stub=True, tests={"test_x.py": source})["passed"] == 2
    # the same tests against the real function fail: the stub really is what changed their outcome
    assert report_of(tmp_path / "real", tests={"test_x.py": source})["failed_count"] == 2


def test_the_stub_is_per_test_so_the_real_function_is_back_without_the_flag(tmp_path: Path) -> None:
    first = report_of(tmp_path / "a", stub=True)
    second = report_of(tmp_path / "b")
    assert first["passed"] < second["passed"]


# --- Options ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [["--stub"], ["--tests", "--batch"], ["--nonsense"], ["--batch", "--stub"]])
def test_unusable_options_are_a_crash_envelope(tmp_path: Path, flags: list[str]) -> None:
    ran = run_mode(tmp_path, flags, {"items": []})
    assert not ran["ok"] and ran["error"].type == "Crash" and "unusable options" in ran["error"].message


# --- --batch ---------------------------------------------------------------------------------------------------------------------


def batch(tmp_path: Path, items: list[dict[str, Any]], **request: Any) -> dict[str, Any]:
    ran = run_mode(tmp_path, ["--batch"], {"items": items, **request})
    assert ran["ok"], ran["error"]
    return ran["result"]


def test_every_item_gets_its_own_outcome_in_order(tmp_path: Path) -> None:
    result = batch(
        tmp_path,
        [
            {"args": {"mode": "ok", "n": 4}},
            {"args": {"mode": "tool_error"}},
            {"args": {"mode": "crash"}},
            {"args": {"mode": "ok", "n": 1}},
        ],
        include_results=True,
    )
    items = result["items"]
    assert [i["index"] for i in items] == [0, 1, 2, 3]
    assert items[0]["ok"] and items[0]["result"] == {"n": 8} and items[0]["result_bytes"] == len('{"n": 8}')
    assert items[1]["error"]["type"] == "ToolError" and "not allowed" in items[1]["error"]["message"]
    assert items[2]["error"]["type"] == "Crash" and "KeyError" in items[2]["error"]["message"]
    assert "tool.py" in items[2]["error"]["trace_tail"]
    assert items[3]["ok"] and items[3]["result"] == {"n": 2}
    assert result["not_run"] == 0
    assert all(i["duration_ms"] >= 0 for i in items)


def test_results_are_left_out_unless_asked_for(tmp_path: Path) -> None:
    item = batch(tmp_path, [{"args": {"mode": "ok", "n": 1}}])["items"][0]
    assert item["ok"] and "result" not in item and item["result_bytes"] > 0


def test_a_slow_item_times_out_and_the_next_one_still_runs(tmp_path: Path) -> None:
    started = time.monotonic()
    result = batch(
        tmp_path,
        [{"args": {"mode": "sleep", "seconds": 30}}, {"args": {"mode": "ok"}}],
        limits={"timeout_s": 1, "output_kb": 16},
    )
    assert time.monotonic() - started < 20
    assert result["items"][0]["error"]["type"] == "Timeout"
    assert result["items"][1]["ok"]


def test_a_call_that_kills_its_own_process_is_a_crash_not_a_hang(tmp_path: Path) -> None:
    items = batch(tmp_path, [{"args": {"mode": "exit"}}, {"args": {"mode": "ok"}}])["items"]
    assert items[0]["error"]["type"] == "Crash" and "without a result" in items[0]["error"]["message"]
    assert items[1]["ok"]


def test_an_oversized_result_is_reported_as_such(tmp_path: Path) -> None:
    item = batch(tmp_path, [{"args": {"mode": "big", "size": 100_000}}])["items"][0]
    assert item["error"]["type"] == "OutputTooLarge"


def test_each_item_gets_only_its_own_files(tmp_path: Path) -> None:
    items = batch(
        tmp_path,
        [
            {"args": {"mode": "count_files"}, "files": {"inputs/a.txt": "1", "inputs/sub/b.txt": "2"}},
            {"args": {"mode": "count_files"}, "files": {"inputs/c.txt": "3"}},
            {"args": {"mode": "count_files"}},
            {"args": {"mode": "read", "path": "inputs/c.txt"}, "files": {"inputs/c.txt": "héllo"}},
        ],
        include_results=True,
    )["items"]
    assert items[0]["result"] == {"files": ["a.txt", "sub"]}
    assert items[1]["result"] == {"files": ["c.txt"]}
    assert items[2]["result"] == {"files": []}
    assert items[3]["result"] == {"text": "héllo"}


@pytest.mark.parametrize(
    "path",
    ["a.txt", "inputs", "inputs/", "inputs/../x", "../inputs/x", "/inputs/x", "inputs//x", "pkg/tool.py"],
)
def test_bad_input_file_names_fail_that_item_only(tmp_path: Path, path: str) -> None:
    items = batch(tmp_path, [{"args": {"mode": "ok"}, "files": {path: "x"}}, {"args": {"mode": "ok"}}])[
        "items"
    ]
    assert not items[0]["ok"] and "not 'inputs/<relative path>'" in items[0]["error"]["message"]
    assert items[1]["ok"]
    assert not (tmp_path / "workspace" / "pkg" / "x").exists()


def test_a_malformed_item_fails_alone(tmp_path: Path) -> None:
    items = batch(tmp_path, [{"files": {}}, "text", {"args": {"mode": "ok"}}])["items"]
    assert not items[0]["ok"] and not items[1]["ok"] and items[2]["ok"]


def test_memory_is_not_shared_between_items(tmp_path: Path) -> None:
    # `write` leaves a file behind (workspace files are shared, memory is not); module state resets every call
    items = batch(tmp_path, [{"args": {"mode": "write"}}, {"args": {"mode": "ok"}}])["items"]
    assert all(i["ok"] for i in items)


def test_processes_a_call_started_do_not_outlive_it(tmp_path: Path) -> None:
    def alive() -> bool:
        listing = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=False).stdout
        return any(line.strip() == "sleep 37" for line in listing.splitlines())

    assert not alive()
    batch(tmp_path, [{"args": {"mode": "child"}}])
    deadline = time.monotonic() + 5
    while alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not alive()


def test_the_budget_stops_the_batch_and_says_how_many_were_not_run(tmp_path: Path) -> None:
    items = [{"args": {"mode": "sleep", "seconds": 0.7}} for _ in range(10)]
    result = batch(tmp_path, items, deadline_s=2)
    assert result["not_run"] > 0 and len(result["items"]) + result["not_run"] == 10


def test_too_many_or_malformed_items_are_a_crash(tmp_path: Path) -> None:
    for request in ({"items": [{"args": {}}] * (harness_batch_cap() + 1)}, {"items": "no"}, {}):
        ran = run_mode(tmp_path / str(len(str(request))), ["--batch"], request)
        assert not ran["ok"] and ran["error"].type == "Crash"


def harness_batch_cap() -> int:
    return 500


def test_the_envelope_is_one_line_with_only_its_own_markers(tmp_path: Path) -> None:
    workspace = workspace_with(tmp_path, None, {"items": [{"args": {"mode": "ok"}}]})
    proc = subprocess.run(
        [sys.executable, str(workspace / ".tf" / "harness.py"), NONCE, "--batch"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        check=False,
    )
    assert proc.stdout.count(harness.start_marker(NONCE)) == 1
    assert proc.stdout.count(b"\n") == 1 and proc.stdout.endswith(b"\n")
