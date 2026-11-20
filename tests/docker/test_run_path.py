"""The whole run path against a real Docker daemon: `ToolRunner` -> `MiniLockerRunner` -> `harness.py` in a container.

Skipped automatically when no daemon answers (see tests/conftest.py). They show what the scripted tests cannot: that the layout the
host writes is the layout the harness reads, that the reference packs really run in the runtime image, that nothing survives from one
call to the next, and that a tool which never returns is stopped and reported as a timeout.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.models.package import ToolPackage
from toolforge.sandbox.minilocker_runner import MiniLockerRunner
from toolforge.services.runner import ToolRunner

pytestmark = [pytest.mark.docker, pytest.mark.usefixtures("minilocker")]

PACKS = Path(__file__).resolve().parent.parent.parent / "examples" / "toolpacks"

PROBE = """\
import os
import socket
import time

MARKERS = ["/tmp/probe_marker", "/workspace/probe_marker"]


def run(args):
    mode = args["mode"]
    if mode == "sleep":
        time.sleep(60)
        return {}
    if mode == "network":
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=2).close()
            return {"reachable": True}
        except OSError:
            return {"reachable": False}
    found = [path for path in MARKERS if os.path.exists(path)]
    for path in MARKERS:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("left behind")
    return {"found": found, "uid": os.getuid()}
"""


@pytest.fixture
def tool_runner(tmp_path: Path) -> Iterator[ToolRunner]:
    settings = Settings(home=tmp_path / "home")
    sandbox = MiniLockerRunner(settings=settings)
    from toolforge.cli_run import make_ledger

    yield ToolRunner(sandbox, None, settings, make_ledger)
    sandbox.shutdown()


@pytest.fixture
def probe(make_payload: Callable[[], dict]) -> ToolPackage:
    payload = make_payload()
    payload["manifest"].update(
        name="probe",
        input_schema={
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        examples=[{"input": {"mode": "probe"}, "output": {"found": [], "uid": 1000}, "live": False}],
        limits={"timeout_s": 2, "memory_mb": 128, "output_kb": 16},
    )
    payload["files"]["tool.py"] = PROBE
    return ToolPackage.from_submission(payload)


@pytest.mark.parametrize("name", ["word_count", "csv_summary"])
def test_a_reference_pack_runs_end_to_end_and_returns_its_manifest_example(
    tool_runner: ToolRunner, name: str
) -> None:
    package = ToolPackage.from_dir(PACKS / name)
    for example in package.manifest.examples:
        files = {path: text.encode() for path, text in (example.files or {}).items()}
        result = tool_runner.run_unverified(package, example.input, files=files)
        assert result.ok, result.error
        assert result.result == example.output
        assert result.duration_ms > 0 and result.sandbox_timed_out is False


def test_a_tool_that_reads_another_calls_data_finds_nothing(
    tool_runner: ToolRunner, probe: ToolPackage
) -> None:
    first = tool_runner.run_unverified(probe, {"mode": "probe"})
    second = tool_runner.run_unverified(probe, {"mode": "probe"})
    assert first.ok and second.ok, (first.error, second.error)
    assert first.result["found"] == [] and second.result["found"] == []  # the first call's markers are gone
    assert first.result["uid"] != 0  # and the tool does not run as root


def test_a_tool_that_never_returns_is_stopped_and_reported_as_a_timeout(
    tool_runner: ToolRunner, probe: ToolPackage
) -> None:
    result = tool_runner.run_unverified(probe, {"mode": "sleep"})
    assert not result.ok and result.error is not None and result.error.type == "Timeout"
    assert result.sandbox_timed_out is True and result.result is None
    assert "/var/" not in result.error.message and "docker" not in result.error.message.lower()


def test_a_tool_has_no_route_out(tool_runner: ToolRunner, probe: ToolPackage) -> None:
    result = tool_runner.run_unverified(probe, {"mode": "network"})
    assert result.ok and result.result == {"reachable": False}


def test_the_ledger_of_a_call_is_one_valid_chain_with_both_sides_in_it(
    tool_runner: ToolRunner, probe: ToolPackage, tmp_path: Path
) -> None:
    result = tool_runner.run_unverified(probe, {"mode": "probe"})
    path = tmp_path / "home" / "ledgers" / "_dev" / "probe@1.0.0" / f"{result.call_id}.jsonl"
    assert path.is_file()
    ledger = ToolforgeLedger(path, result.call_id)
    ok, why = ledger.verify()
    assert ok, why
    types = [event["type"] for event in ledger.events()]
    assert types[0] == "forge.call.started" and types[-1] == "forge.call.finished"
    assert "probe_marker" not in path.read_text(encoding="utf-8")  # no argument or result value is recorded
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])  # one JSON object per line
