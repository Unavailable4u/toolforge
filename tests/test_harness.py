"""The in-sandbox harness, run for real, but only on our own checked-in fixtures (`tests/fixtures/trusted_tools/`).

This is the one place where a unit test executes tool code outside a sandbox. It is allowed because the code is ours; a generated
or submitted package is never run this way (the runner's own tests use a scripted sandbox). Each test builds the layout the host
builds in a real sandbox (`pkg/`, `inputs/`, `.tf/harness.py`, `.tf/in.json`, `.tf/toolforge_sdk.py`) in a temporary folder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from toolforge.sandbox import harness
from toolforge.services.runner import parse_envelope

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")

TRUSTED = Path(__file__).parent / "fixtures" / "trusted_tools"
ASSET = Path(__file__).resolve().parent.parent / "src" / "toolforge" / "sandbox" / "assets" / "harness.py"
SDK = Path(__file__).resolve().parent.parent / "src" / "toolforge" / "sdk.py"
NONCE = "ab" * 16


class Ran:
    """One harness run: the process, the raw stdout and the envelope as a plain dict (None when there was none)."""

    def __init__(self, proc: subprocess.CompletedProcess[bytes]) -> None:
        self.proc = proc
        self.stdout = proc.stdout
        start, end = harness.start_marker(NONCE), harness.end_marker(NONCE)
        at = self.stdout.rfind(start)
        stop = self.stdout.find(end, at)
        self.raw: dict[str, Any] | None = (
            json.loads(self.stdout[at + len(start) : stop]) if at >= 0 and stop >= 0 else None
        )

    @property
    def ok(self) -> bool:
        assert self.raw is not None
        return bool(self.raw["ok"])

    @property
    def result(self) -> Any:
        assert self.raw is not None
        return self.raw["result"]

    @property
    def error(self) -> dict[str, Any]:
        assert self.raw is not None and self.raw["error"] is not None
        return self.raw["error"]


def run_harness(
    tmp_path: Path,
    fixture: str,
    args: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    inputs: dict[str, bytes] | None = None,
    entrypoint: str = "tool.py:run",
    request: bytes | None = None,
    env: dict[str, str] | None = None,
) -> Ran:
    workspace = tmp_path / "workspace"
    shutil.copytree(TRUSTED / fixture, workspace / "pkg")
    tf = workspace / ".tf"
    tf.mkdir()
    (tf / "harness.py").write_text(harness.HARNESS_SOURCE, encoding="utf-8")
    shutil.copy(SDK, tf / "toolforge_sdk.py")
    body = {
        "args": args,
        "entrypoint": entrypoint,
        "limits": {"timeout_s": 10, "output_kb": 16} if limits is None else limits,
    }
    if request is not None:
        (tf / "in.json").write_bytes(request)
    else:
        (tf / "in.json").write_text(json.dumps(body), encoding="utf-8")
    for name, data in (inputs or {}).items():
        target = workspace / "inputs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    clean_env = {"PATH": os.environ.get("PATH", ""), **(env or {})}
    proc = subprocess.run(
        [sys.executable, str(tf / "harness.py"), NONCE],
        capture_output=True,
        timeout=60,
        env=clean_env,
        cwd=tmp_path,
        check=False,
    )
    return Ran(proc)


def behave(tmp_path: Path, mode: str, **more: Any) -> Ran:
    extra = {k: more.pop(k) for k in ("limits", "inputs", "entrypoint", "request", "env") if k in more}
    return run_harness(tmp_path, "behaviours", {"mode": mode, **more}, **extra)


# --- The asset copy and the constant ---------------------------------------------------------------------------------------


def test_the_asset_copy_equals_the_constant() -> None:
    assert ASSET.read_text(encoding="utf-8") == harness.HARNESS_SOURCE


def test_the_constant_is_what_the_host_can_embed() -> None:
    assert "'''" not in ASSET.read_text(encoding="utf-8")  # the constant is a raw triple single quoted string
    compile(harness.HARNESS_SOURCE, "harness.py", "exec")


def test_the_harness_imports_only_the_standard_library() -> None:
    import ast

    tree = ast.parse(harness.HARNESS_SOURCE)
    roots = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    roots |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    allowed = {
        "__future__",
        "importlib",
        "io",
        "json",
        "os",
        "sys",
        "time",
        "traceback",
        "resource",
        "toolforge_sdk",
    }
    assert roots <= allowed, roots - allowed
    assert "toolforge" not in roots


def test_the_markers_match_the_documented_shape() -> None:
    assert harness.start_marker("n1") == b"<<TF:n1>>"
    assert harness.end_marker("n1") == b"<<TF-END:n1>>"


# --- Outcomes ------------------------------------------------------------------------------------------------------------------


def test_success_returns_the_value_and_exits_cleanly(tmp_path: Path) -> None:
    ran = behave(tmp_path, "ok")
    assert ran.ok and ran.result == {"words": 2}
    assert ran.raw is not None and ran.raw["error"] is None
    assert ran.proc.returncode == 0
    assert isinstance(ran.raw["duration_ms"], int) and ran.raw["truncated"] is False


def test_the_envelope_is_one_ascii_line_with_only_its_own_markers(tmp_path: Path) -> None:
    ran = behave(tmp_path, "unicode")
    lines = [line for line in ran.stdout.split(b"\n") if line]
    assert len(lines) == 1
    line = lines[0]
    line.decode("ascii")  # unicode is escaped, so the line is plain ASCII
    assert line.startswith(harness.start_marker(NONCE)) and line.endswith(harness.end_marker(NONCE))
    body = line[len(harness.start_marker(NONCE)) : -len(harness.end_marker(NONCE))]
    assert b"<" not in body and b">" not in body


def test_unicode_survives_the_round_trip(tmp_path: Path) -> None:
    ran = behave(tmp_path, "unicode")
    assert ran.result == "h\u00e9llo \u2713 \u65e5\u672c \U0001f600"
    assert ran.raw is not None and ran.raw["stdout_tail"] == "h\u00e9llo \u2713 \u65e5\u672c\n"


def test_a_tool_error_is_a_handled_failure(tmp_path: Path) -> None:
    ran = behave(tmp_path, "tool_error")
    assert not ran.ok and ran.result is None
    assert ran.error["type"] == "ToolError" and ran.error["message"] == "the file is not a CSV"
    assert ran.error["trace_tail"] == ""  # expected failures carry no traceback


def test_any_other_exception_is_a_crash_with_a_trace_that_starts_in_the_tool(tmp_path: Path) -> None:
    ran = behave(tmp_path, "crash")
    assert not ran.ok
    assert ran.error["type"] == "Crash" and "KeyError" in ran.error["message"]
    trace = ran.error["trace_tail"]
    assert "KeyError" in trace and "tool.py" in trace
    assert "harness.py" not in trace  # the harness's own frame says nothing useful


@pytest.mark.parametrize("mode", ["set", "nan", "surrogate", "cycle", "deep"])
def test_a_result_that_is_not_json_is_not_json(tmp_path: Path, mode: str) -> None:
    ran = behave(tmp_path, mode)
    assert not ran.ok and ran.result is None
    assert ran.error["type"] == "NotJson"
    assert ran.error["message"]


def test_a_result_over_the_limit_is_output_too_large(tmp_path: Path) -> None:
    ran = behave(tmp_path, "oversized", size=5000, limits={"timeout_s": 10, "output_kb": 1})
    assert not ran.ok
    assert ran.error["type"] == "OutputTooLarge" and "limits.output_kb is 1" in ran.error["message"]


def test_a_result_at_the_limit_is_returned(tmp_path: Path) -> None:
    ran = behave(tmp_path, "oversized", size=900, limits={"timeout_s": 10, "output_kb": 1})
    assert ran.ok and len(ran.result) == 900


def test_without_an_output_limit_the_default_applies(tmp_path: Path) -> None:
    ran = behave(tmp_path, "oversized", size=harness.DEFAULT_OUTPUT_KB * 1024 + 1, limits={})
    assert not ran.ok and ran.error["type"] == "OutputTooLarge"


def test_system_exit_is_a_crash_not_a_silent_exit(tmp_path: Path) -> None:
    ran = behave(tmp_path, "exit")
    assert not ran.ok and ran.error["type"] == "Crash" and "SystemExit" in ran.error["message"]


def test_a_tool_that_kills_the_process_leaves_no_envelope(tmp_path: Path) -> None:
    ran = behave(tmp_path, "os_exit")
    assert ran.raw is None  # the host maps this to NoResult


# --- A hostile tool cannot confuse the envelope ---------------------------------------------------------------------------------


def test_a_printed_envelope_is_only_captured_text(tmp_path: Path) -> None:
    ran = behave(tmp_path, "prints_marker", nonce=NONCE)
    assert ran.ok and ran.result == "honest"
    assert ran.stdout.count(harness.start_marker(NONCE)) == 1  # the print is escaped inside stdout_tail
    assert ran.raw is not None and "forged" in ran.raw["stdout_tail"]
    parsed = parse_envelope(ran.stdout, NONCE)
    assert parsed is not None and parsed.result == "honest"


def test_an_envelope_written_to_fd_1_loses_to_the_real_one(tmp_path: Path) -> None:
    ran = behave(tmp_path, "writes_fd1", nonce=NONCE)
    assert ran.stdout.count(harness.start_marker(NONCE)) == 2  # the forgery is there, the real one comes last
    parsed = parse_envelope(ran.stdout, NONCE)
    assert parsed is not None and parsed.result == "honest"


def test_a_tool_that_prints_a_lot_gets_short_tails_and_a_truncated_flag(tmp_path: Path) -> None:
    ran = behave(tmp_path, "prints_a_lot")
    assert ran.ok and ran.result == "done"
    assert ran.raw is not None
    assert ran.raw["truncated"] is True
    assert (
        len(ran.raw["stdout_tail"]) <= harness.TAIL_CHARS
        and len(ran.raw["stderr_tail"]) <= harness.TAIL_CHARS
    )
    assert ran.raw["stdout_tail"].endswith("x\n") and ran.raw["stderr_tail"].endswith("e\n")
    assert len(ran.stdout) < 20_000  # nothing the tool printed reached the real stdout


@pytest.mark.parametrize("fixture_mode", ["report", "error"])
def test_a_package_cannot_replace_the_standard_library_or_the_sdk(tmp_path: Path, fixture_mode: str) -> None:
    ran = run_harness(tmp_path, "shadowing", {"mode": fixture_mode})
    if fixture_mode == "report":
        assert ran.ok and ran.result == {"json_is_shadow": False, "sdk_is_shadow": False}
    else:
        assert ran.error["type"] == "ToolError"  # the real SDK's ToolError, so the harness recognised it


def test_vendored_dependencies_are_importable_from_pkg_deps(tmp_path: Path) -> None:
    ran = run_harness(tmp_path, "vendored", {})
    assert ran.ok and ran.result == {"answer": 42}


# --- The environment a tool sees -------------------------------------------------------------------------------------------------


def test_the_working_directory_is_the_workspace_so_relative_input_paths_resolve(tmp_path: Path) -> None:
    ran = behave(tmp_path, "read_relative", path="inputs/data.csv", inputs={"data.csv": b"a,b\n1,2\n"})
    assert ran.ok and ran.result == "a,b\n1,2\n"
    where = behave(tmp_path / "again", "env")
    assert where.result["cwd"] == str(tmp_path / "again" / "workspace")
    assert where.result["argv0"] == str(tmp_path / "again" / "workspace" / "pkg" / "tool.py")


def test_input_path_accepts_the_name_with_or_without_inputs(tmp_path: Path) -> None:
    for name in ("data.csv", "inputs/data.csv"):
        ran = behave(tmp_path / name.replace("/", "_"), "input_path", name=name, inputs={"data.csv": b"x"})
        assert ran.ok and ran.result == "x"


def test_input_path_refuses_a_missing_file_and_a_way_out_and_lists_what_exists(tmp_path: Path) -> None:
    missing = behave(tmp_path / "a", "input_path", name="nope.csv", inputs={"data.csv": b"x"})
    assert missing.error["type"] == "ToolError"
    assert "nope.csv" in missing.error["message"] and "data.csv" in missing.error["message"]
    outside = behave(tmp_path / "b", "input_path", name="../pkg/tool.py", inputs={"data.csv": b"x"})
    assert outside.error["type"] == "ToolError" and ".." in outside.error["message"]


def test_the_hash_seed_is_fixed_so_set_order_repeats(tmp_path: Path) -> None:
    first = behave(tmp_path / "one", "set_order", env={"PYTHONHASHSEED": "random"})
    second = behave(tmp_path / "two", "set_order", env={})
    assert first.ok and second.ok and first.result == second.result
    seed = behave(tmp_path / "three", "env", env={"PYTHONHASHSEED": "random"})
    assert seed.result["hash_seed"] == "0"


# --- Bad requests --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrypoint",
    [
        "../tool.py:run",
        "/etc/x.py:run",
        "tool.py",
        "tool.py:",
        "tool.txt:run",
        "missing.py:run",
        "tool.py:nope",
    ],
)
def test_a_bad_entrypoint_is_a_crash_with_a_message(tmp_path: Path, entrypoint: str) -> None:
    ran = behave(tmp_path, "ok", entrypoint=entrypoint)
    assert not ran.ok and ran.error["type"] == "Crash" and ran.error["message"]


def test_an_unreadable_request_is_a_crash_not_silence(tmp_path: Path) -> None:
    ran = behave(tmp_path, "ok", request=b"{not json")
    assert not ran.ok and ran.error["type"] == "Crash"
    assert ran.proc.returncode == 0
