"""The harness's session protocol (P13), run for real on code this file writes: load and save `state.json`, the 256 KB cap, `--state`.

Same rules as `tests/test_harness.py`: trusted test code only, never a submitted package.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from toolforge.sandbox import harness
from toolforge.services.runner import parse_envelope

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")

SDK = Path(__file__).resolve().parent.parent / "src" / "toolforge" / "sdk.py"
NONCE = "ef" * 16
TOOL = """\
import os

from toolforge_sdk import ToolError


def run(args, state):
    if args.get("fail"):
        state["half"] = "written"
        raise ToolError("no")
    if args.get("garbage"):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tf", "state.json"), "w") as f:
            f.write("{not json")
        return {"ok": True}
    if args.get("list"):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tf", "state.json"), "w") as f:
            f.write("[1, 2]")
        return {"ok": True}
    if args.get("huge"):
        state["x"] = "y" * 270_000
    state["total"] = state.get("total", 0) + args.get("add", 0)
    return {"total": state["total"], "files": sorted(os.listdir("inputs")) if os.path.isdir("inputs") else []}
"""
STATELESS = "def run(args):\n    return {'seen': sorted(args)}\n"


def workspace(tmp_path: Path, tool: str = TOOL) -> Path:
    ws = tmp_path / "workspace"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "tool.py").write_text(tool, encoding="utf-8")
    (ws / ".tf").mkdir()
    (ws / ".tf" / "harness.py").write_text(harness.HARNESS_SOURCE, encoding="utf-8")
    (ws / ".tf" / "toolforge_sdk.py").write_text(SDK.read_text(encoding="utf-8"), encoding="utf-8")
    return ws


def invoke(ws: Path, request: dict[str, Any], *flags: str):
    base = {"entrypoint": "tool.py:run", "limits": {"timeout_s": 10, "output_kb": 16}}
    (ws / ".tf" / "in.json").write_text(json.dumps({**base, **request}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ws / ".tf" / "harness.py"), NONCE, *flags],
        capture_output=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", "")},
        cwd=ws.parent,
        check=False,
    )
    return proc, parse_envelope(proc.stdout, NONCE)


def state_of(ws: Path) -> dict[str, Any]:
    return json.loads((ws / ".tf" / "state.json").read_text(encoding="utf-8"))


def test_a_session_call_loads_the_saved_state_and_saves_what_changed(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    for expected in (2, 5, 6):
        _, envelope = invoke(
            ws,
            {
                "args": {
                    "add": expected - (state_of(ws)["total"] if (ws / ".tf/state.json").exists() else 0)
                },
                "session": True,
            },
        )
        assert envelope is not None and envelope.ok and envelope.result["total"] == expected
    assert state_of(ws) == {"total": 6}


def test_the_call_reports_the_hash_and_size_of_the_state_it_saved(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    _, envelope = invoke(ws, {"args": {"add": 3}, "session": True})
    canonical = json.dumps({"total": 3}, sort_keys=True, ensure_ascii=False).encode()
    assert envelope.state_sha256 == hashlib.sha256(canonical).hexdigest() and envelope.state_bytes == len(
        canonical
    )


def test_a_failed_call_saves_nothing(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    invoke(ws, {"args": {"add": 1}, "session": True})
    _, envelope = invoke(ws, {"args": {"fail": True}, "session": True})
    assert not envelope.ok and envelope.error.type == "ToolError"
    assert state_of(ws) == {"total": 1}  # "half" was never written


def test_state_over_256_kb_fails_the_call_with_a_clear_error_and_is_not_saved(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    invoke(ws, {"args": {"add": 1}, "session": True})
    _, envelope = invoke(ws, {"args": {"huge": True}, "session": True})
    assert not envelope.ok and envelope.error.type == "OutputTooLarge" and "256 KB" in envelope.error.message
    assert state_of(ws) == {"total": 1}


@pytest.mark.parametrize("which", ["garbage", "list"])
def test_a_state_file_that_is_not_a_json_object_fails_the_next_call_clearly(
    tmp_path: Path, which: str
) -> None:
    ws = workspace(tmp_path)
    invoke(ws, {"args": {which: True}, "session": True})
    _, envelope = invoke(ws, {"args": {"add": 1}, "session": True})
    assert not envelope.ok and "session state" in envelope.error.message


def test_an_oversized_state_file_is_refused_on_load(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / ".tf" / "state.json").write_text(json.dumps({"x": "y" * 300_000}), encoding="utf-8")
    _, envelope = invoke(ws, {"args": {"add": 1}, "session": True})
    assert not envelope.ok and "256 KB" in envelope.error.message


def test_only_the_files_sent_with_a_session_call_are_visible_to_it(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "inputs").mkdir()
    (ws / "inputs" / "old.txt").write_text("from an earlier call", encoding="utf-8")
    (ws / "inputs" / "new.txt").write_text("now", encoding="utf-8")
    _, envelope = invoke(ws, {"args": {}, "session": True, "input_files": ["inputs/new.txt"]})
    assert envelope.result["files"] == ["new.txt"]


def test_state_mode_reports_the_saved_state_without_running_the_tool(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    _, empty = invoke(ws, {}, "--state")
    assert (
        empty.result["state_sha256"] == hashlib.sha256(b"{}").hexdigest() and empty.result["state_bytes"] == 2
    )
    invoke(ws, {"args": {"add": 4}, "session": True})
    _, now = invoke(ws, {}, "--state")
    assert (
        now.result["state_sha256"]
        == hashlib.sha256(json.dumps({"total": 4}, sort_keys=True).encode()).hexdigest()
    )


def test_state_mode_does_not_combine_with_other_modes(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    _, envelope = invoke(ws, {}, "--state", "--batch")
    assert envelope is not None and not envelope.ok and "unusable options" in envelope.error.message


def test_a_stateless_call_is_still_called_with_args_alone_and_keeps_no_state(tmp_path: Path) -> None:
    ws = workspace(tmp_path, STATELESS)
    _, envelope = invoke(ws, {"args": {"a": 1}})
    assert envelope.ok and envelope.result == {"seen": ["a"]} and not (ws / ".tf" / "state.json").exists()
    assert envelope.state_sha256 is None


def test_a_batch_in_session_mode_shares_one_state_in_order(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    items = [{"args": {"add": n}, "files": {}} for n in (1, 2, 3)]
    _, envelope = invoke(
        ws, {"items": items, "include_results": True, "deadline_s": 30, "session": True}, "--batch"
    )
    assert envelope.ok
    assert [i["result"]["total"] for i in envelope.result["items"]] == [1, 3, 6]
    _, plain = invoke(ws, {"items": items, "include_results": True, "deadline_s": 30}, "--batch")
    assert (
        plain.ok and not plain.result["items"][0]["ok"]
    )  # without the session flag the tool is called with the wrong arity
