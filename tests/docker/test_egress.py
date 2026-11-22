"""The egress proxy against a real Docker daemon: a tool with approved hosts reaches them and only them.

These need Docker and the internet (the proxy only lets public addresses through, so no local server can stand in). They are skipped
automatically when no Docker daemon answers (see tests/conftest.py). The brokered-secret path needs a real service and token and is
not covered here: it is covered by `tests/test_egress_broker.py` (the configuration, the pool) and `tests/test_runner_permissions.py`
(the runner), with the proxy replaced by a stand-in.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from toolforge.sandbox.base import SandboxSpec
from toolforge.sandbox.minilocker_runner import MiniLockerRunner

pytestmark = [pytest.mark.docker, pytest.mark.usefixtures("minilocker")]

IMAGE = "python:3.12-slim"
FETCH = """
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=20) as r:
        print("status", r.status)
except Exception as exc:
    print("error", type(exc).__name__)
"""


@pytest.fixture
def runner() -> Iterator[MiniLockerRunner]:
    runner = MiniLockerRunner()
    yield runner
    runner.shutdown()


def fetch(handle, url: str) -> str:
    handle.put("fetch.py", FETCH.encode())
    out = handle.run(["python", "fetch.py", url], 60, 10_000)
    return out.stdout.decode().strip()


def test_an_approved_host_is_reachable_and_an_unlisted_one_is_not(
    runner: MiniLockerRunner, tmp_path: Path
) -> None:
    ledger = tmp_path / "run.jsonl"
    handle = runner.open(SandboxSpec(image=IMAGE, network=("pypi.org",), ledger_path=str(ledger)))
    try:
        assert fetch(handle, "https://pypi.org/simple/").startswith("status 200")
        assert fetch(handle, "https://example.com/").startswith("error")
    finally:
        handle.close()
    events = [e for e in handle.events() if str(e.get("type", "")).startswith("egress.")]
    by_host = {(e["type"], (e.get("payload") or {}).get("host")) for e in events}
    assert ("egress.allowed", "pypi.org") in by_host
    assert ("egress.blocked", "example.com") in by_host


def test_a_sandbox_with_no_network_has_no_route_even_after_one_with_a_network_ran(
    runner: MiniLockerRunner, tmp_path: Path
) -> None:
    first = runner.open(
        SandboxSpec(image=IMAGE, network=("pypi.org",), ledger_path=str(tmp_path / "a.jsonl"))
    )
    first.close()
    second = runner.open(SandboxSpec(image=IMAGE, network="none", ledger_path=str(tmp_path / "b.jsonl")))
    try:
        assert fetch(second, "https://pypi.org/simple/").startswith("error")
    finally:
        second.close()
