"""`MiniLockerRunner` against a real Docker daemon. Skipped automatically when none answers (see tests/conftest.py).

These are the tests that show the hardening is real: Docker's own account of the container is read back, and the commands
run inside really cannot write the root filesystem or reach the network. They pull `python:3.12-slim` on first use.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from toolforge.errors import SandboxError
from toolforge.sandbox import _ml_path
from toolforge.sandbox.base import SandboxSpec
from toolforge.sandbox.minilocker_runner import MiniLockerHandle, MiniLockerRunner

pytestmark = [pytest.mark.docker, pytest.mark.usefixtures("minilocker")]

IMAGE = "python:3.12-slim"


@pytest.fixture
def runner() -> Iterator[MiniLockerRunner]:
    runner = MiniLockerRunner()
    yield runner
    runner.shutdown()


@pytest.fixture
def handle(runner: MiniLockerRunner, tmp_path: Path) -> Iterator[MiniLockerHandle]:
    opened = runner.open(SandboxSpec(image=IMAGE, network="none", ledger_path=str(tmp_path / "run.jsonl")))
    yield opened
    opened.close()  # idempotent: a test that closed it already is not an error


def container_exists(name: str) -> bool:
    import docker

    try:
        docker.from_env().containers.get(name)
        return True
    except docker.errors.NotFound:
        return False


def test_put_then_run_sees_the_file_and_its_output(handle: MiniLockerHandle) -> None:
    handle.put("tool/hello.py", b"print('hello from the box')")
    out = handle.run(["python", "tool/hello.py"], 30, 10_000)
    assert (out.exit_code, out.stdout.strip(), out.timed_out) == (0, b"hello from the box", False)


def test_put_creates_missing_directories_and_overwrites(handle: MiniLockerHandle) -> None:
    handle.put("/workspace/a/b/c.txt", b"one")
    handle.put("/workspace/a/b/c.txt", b"two")
    assert handle.run(["cat", "/workspace/a/b/c.txt"], 10, 100).stdout == b"two"


def test_a_path_with_shell_characters_is_a_file_name_not_a_command(handle: MiniLockerHandle) -> None:
    handle.put("/workspace/$(touch pwned);x", b"data")
    assert handle.run(["ls", "/workspace"], 10, 1000).stdout.decode().split("\n")[:1] == ["$(touch pwned);x"]
    assert handle.run(["test", "-e", "/workspace/pwned"], 10, 100).exit_code != 0


def test_output_is_capped_and_marked_truncated(handle: MiniLockerHandle) -> None:
    out = handle.run(["python", "-c", "print('x' * 100000)"], 30, 1000)
    assert out.truncated is True and len(out.stdout) <= 1000


def test_network_none_has_no_route_out(handle: MiniLockerHandle) -> None:
    code = "import socket; socket.create_connection(('1.1.1.1', 53), 3)"
    out = handle.run(["python", "-c", code], 20, 1000)
    assert out.exit_code != 0 and b"Traceback" in out.stderr


def test_the_root_filesystem_is_read_only_and_the_workspace_is_writable(handle: MiniLockerHandle) -> None:
    assert handle.run(["touch", "/etc/forge-test"], 10, 1000).exit_code != 0
    assert handle.run(["touch", "/workspace/ok"], 10, 1000).exit_code == 0
    assert handle.run(["touch", "/tmp/ok"], 10, 1000).exit_code == 0


def test_the_process_is_not_root(handle: MiniLockerHandle) -> None:
    assert handle.run(["id", "-u"], 10, 100).stdout.strip() == b"65534"


def test_the_memory_limit_is_enforced_by_the_container(runner: MiniLockerRunner, tmp_path: Path) -> None:
    with runner.open(
        SandboxSpec(image=IMAGE, network="none", memory_mb=64, ledger_path=str(tmp_path / "m.jsonl"))
    ) as box:
        out = box.run(["python", "-c", "b = bytearray(400 * 1024 * 1024)"], 30, 1000)
    assert out.exit_code != 0  # killed or MemoryError: the allocation did not succeed


def test_freeze_and_thaw_round_trip_into_a_fresh_sandbox(runner: MiniLockerRunner, tmp_path: Path) -> None:
    with runner.open(
        SandboxSpec(image=IMAGE, network="none", ledger_path=str(tmp_path / "a.jsonl"))
    ) as first:
        first.put("data/keep.txt", b"kept")
        blob = first.freeze()
    assert blob[:2] == b"\x1f\x8b"  # gzip
    with runner.open(
        SandboxSpec(image=IMAGE, network="none", ledger_path=str(tmp_path / "b.jsonl"))
    ) as second:
        second.thaw(blob)
        assert second.run(["cat", "/workspace/data/keep.txt"], 10, 100).stdout == b"kept"


def test_thaw_of_garbage_is_an_error(handle: MiniLockerHandle) -> None:
    with pytest.raises(SandboxError, match="could not restore"):
        handle.thaw(b"this is not a gzip archive")


def test_a_command_past_its_timeout_kills_the_container(handle: MiniLockerHandle) -> None:
    name = handle.name
    out = handle.run(["sleep", "30"], 2, 100)
    assert out.timed_out is True
    with pytest.raises(SandboxError, match="killed"):
        handle.run(["echo", "again"], 5, 100)
    handle.close()
    assert not container_exists(name)


def test_close_removes_the_container_and_reports_hardening_as_docker_sees_it(
    handle: MiniLockerHandle,
) -> None:
    name = handle.name
    assert container_exists(name)
    summary = handle.close()
    assert summary.destroyed is True and not container_exists(name)
    h = summary.hardening
    assert h["network_mode"] == "none" and h["read_only_rootfs"] is True
    assert h["cap_drop"] == ["ALL"] and h["cap_add"] == [] and h["privileged"] is False
    assert "no-new-privileges" in h["security_opt"] and h["user"] == "65534:65534"
    assert h["binds"] == [] and h["pid_mode"] == "" and h["ipc_mode"] != "host"


def test_the_ledger_records_created_and_destroyed_and_the_chain_verifies(handle: MiniLockerHandle) -> None:
    handle.close()
    types = [e["type"] for e in handle.events()]
    assert types[0] == "sandbox.created" and types[-1] == "sandbox.destroyed" and "sandbox.audit" in types
    ok, message = handle._ledger.verify()
    assert ok, message


def test_decoys_are_planted_and_a_read_of_one_is_noticed(runner: MiniLockerRunner, tmp_path: Path) -> None:
    canary = _ml_path.load("minilocker.trust.canary")
    spec = SandboxSpec(image=IMAGE, network="none", decoys=True, ledger_path=str(tmp_path / "d.jsonl"))
    with runner.open(spec) as box:
        assert (
            box.run(["test", "-f", canary.AWS_PATH], 10, 100).exit_code == 0
        )  # planted where an attacker would look
        box.run(["cat", canary.AWS_PATH], 10, 1000)
        summary = box.close()
    kinds = {e["type"] for e in summary.decoy_events}
    assert "canary.seeded" in kinds and ("canary.read" in kinds or "canary.exposed" in kinds)


def test_decoy_values_are_not_in_the_decoy_events(runner: MiniLockerRunner, tmp_path: Path) -> None:
    canary = _ml_path.load("minilocker.trust.canary")
    spec = SandboxSpec(image=IMAGE, network="none", decoys=True, ledger_path=str(tmp_path / "e.jsonl"))
    with runner.open(spec) as box:
        value = box.run(["cat", canary.DOTENV_PATH], 10, 1000).stdout.decode()
        summary = box.close()
    secret = value.strip().split("=", 1)[-1].strip().strip("'\"")
    assert secret and secret not in str(summary.decoy_events)  # ids and labels only


def test_egress_to_an_allowed_host_is_seen_and_a_blocked_one_is_refused(
    runner: MiniLockerRunner, tmp_path: Path
) -> None:
    spec = SandboxSpec(image=IMAGE, network=("example.com",), ledger_path=str(tmp_path / "n.jsonl"))
    script = (
        "import urllib.request as r\n"
        "for u in ('http://example.com', 'http://not-allowed.invalid'):\n"
        "    try: r.urlopen(u, timeout=8)\n"
        "    except Exception: pass\n"
    )
    with runner.open(spec) as box:
        box.put("probe.py", script.encode())
        box.run(["python", "probe.py"], 40, 4000)
        summary = box.close()
    decisions = {(e["payload"].get("host"), e["type"]) for e in summary.egress_attempts}
    assert ("not-allowed.invalid", "egress.blocked") in decisions
    assert not any(host == "not-allowed.invalid" and kind == "egress.allowed" for host, kind in decisions)
