"""`MiniLockerRunner` with a stand-in `Sandbox` factory: the real MiniLocker `Watch` and `Ledger`, no Docker.

What a real container does (read-only root, no network, a killed command) is proven in `tests/docker/test_minilocker_runner.py`.
Here: what the adapter passes to the box, what it writes to the ledger, and that it always cleans up.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from toolforge.errors import SandboxError, SandboxUnavailable
from toolforge.sandbox import _ml_path
from toolforge.sandbox.base import SandboxSpec
from toolforge.sandbox.minilocker_runner import _PUT_SCRIPT, EgressPool, MiniLockerRunner

pytestmark = pytest.mark.usefixtures("minilocker")


class NotFound(Exception):  # matched by class name, like docker.errors.NotFound
    pass


@dataclass
class Raw:
    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    truncated: bool = False


class StandInBox:
    """Quacks like minilocker's `Sandbox`. `supports_measure` is False, so the real `Watch` is inert unless a test swaps it."""

    supports_measure = False
    _counter = 0

    def __init__(self, **kwargs: Any) -> None:
        StandInBox._counter += 1
        self.kwargs = kwargs
        self.name = f"minilocker-standin{StandInBox._counter}"
        self.ip = f"10.9.0.{StandInBox._counter % 250 + 2}"
        self.dead = False
        self.destroyed = 0
        self.destroy_error: Exception | None = None
        self.hardening_value: dict = {"network_mode": "none", "read_only_rootfs": True}
        self.exec_calls: list[tuple[str, Any]] = []
        self.next_capped = Raw(stdout=b"out")
        self.next_stdin = Raw()

    def hardening(self) -> dict:
        return dict(self.hardening_value)

    def exec_capped(self, argv: list, timeout_s: int = 30, max_bytes: int = 0) -> Raw:
        self.exec_calls.append(("capped", {"argv": argv, "timeout_s": timeout_s, "max_bytes": max_bytes}))
        return self.next_capped

    def exec_stdin(self, argv: list, data: bytes, timeout_s: int = 60, max_out: int = 0) -> Raw:
        self.exec_calls.append(
            ("stdin", {"argv": argv, "data": data, "timeout_s": timeout_s, "max_out": max_out})
        )
        return self.next_stdin

    def destroy(self) -> None:
        self.destroyed += 1
        if self.destroy_error is not None:
            raise self.destroy_error


class Factory:
    def __init__(self, error: Exception | None = None) -> None:
        self.boxes: list[StandInBox] = []
        self.error = error

    def __call__(self, **kwargs: Any) -> StandInBox:
        if self.error is not None:
            raise self.error
        box = StandInBox(**kwargs)
        self.boxes.append(box)
        return box


class FakeManager:
    def __init__(self, hosts: list[str]) -> None:
        self.hosts = hosts
        self.events: list[dict] = []
        self.fail_reads = False
        self.read_args: list[tuple[str, float]] = []
        self.stopped = False

    def events_for(self, ip: str, since: float) -> list[dict]:
        self.read_args.append((ip, since))
        if self.fail_reads:
            raise ConnectionError("proxy gone")
        return list(self.events)

    def stop(self) -> None:
        self.stopped = True


def spec(tmp_path: Path, **kw: Any) -> SandboxSpec:
    kw.setdefault("image", "python:3.12-slim")
    kw.setdefault("network", "none")
    kw.setdefault("ledger_path", str(tmp_path / "run.jsonl"))
    return SandboxSpec(**kw)


def make(
    factory: Factory | None = None, pool: EgressPool | None = None
) -> tuple[MiniLockerRunner, Factory, EgressPool]:
    factory = factory or Factory()
    pool = pool or EgressPool(FakeManager)
    return MiniLockerRunner(pool=pool, sandbox_factory=factory), factory, pool


def types(handle: Any) -> list[str]:
    return [e["type"] for e in handle.events()]


def payloads(handle: Any, type_: str) -> list[dict]:
    return [e["payload"] for e in handle.events() if e["type"] == type_]


# -- open: what the box is asked for ---------------------------------------------------------------------------------------------


def test_open_passes_the_limits_and_environment_to_the_box(tmp_path: Path) -> None:
    runner, factory, _ = make()
    runner.open(spec(tmp_path, workspace_mb=32, memory_mb=128, pids=64, env={"A": "1"}))
    assert factory.boxes[0].kwargs == {
        "image": "python:3.12-slim",
        "workspace_mb": 32,
        "egress": None,
        "mem_limit": "128m",
        "pids_limit": 64,
        "extra_env": {"A": "1"},
    }


def test_network_none_never_touches_the_proxy(tmp_path: Path) -> None:
    runner, _, pool = make(pool=EgressPool(lambda hosts: pytest.fail("no proxy may start for network none")))
    handle = runner.open(spec(tmp_path))
    handle.close()
    assert pool.users == 0 and pool.allowed == frozenset()


def test_named_hosts_acquire_the_proxy_and_close_releases_it(tmp_path: Path) -> None:
    runner, factory, pool = make()
    handle = runner.open(spec(tmp_path, network=("Example.com", "api.example.org")))
    assert pool.users == 1 and pool.allowed == {"example.com", "api.example.org"}
    assert isinstance(factory.boxes[0].kwargs["egress"], FakeManager)
    handle.close()
    assert pool.users == 0


def test_a_factory_failure_releases_the_pool_and_is_explained(tmp_path: Path) -> None:
    runner, _, pool = make(Factory(error=OSError("socket closed")))
    with pytest.raises(SandboxUnavailable) as caught:
        runner.open(spec(tmp_path, network=("example.com",)))
    assert "toolforge doctor" in (caught.value.hint or "") and "OSError" in str(caught.value)
    assert pool.users == 0


def test_open_rejects_something_that_is_not_a_spec() -> None:
    with pytest.raises(TypeError):
        make()[0].open({"image": "x"})  # type: ignore[arg-type]


def test_sandbox_created_records_name_hardening_and_a_clean_label(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path, label="verify:csv@1\x1b[31m​"))
    (created,) = payloads(handle, "sandbox.created")
    box = factory.boxes[0]
    assert created["name"] == box.name and created["ip"] == box.ip
    assert created["hardening"] == {"network_mode": "none", "read_only_rootfs": True}
    assert created["label"] == "verify:csv@1[31m"  # control and invisible characters are gone


def test_the_ledger_file_is_written_before_any_container_exists(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "deep" / "er"
    runner, factory, _ = make()
    runner.open(spec(tmp_path, ledger_path=str(ledger_dir / "r.jsonl"))).close()
    assert ledger_dir.is_dir() and len(factory.boxes) == 1


def test_a_bad_ledger_creates_no_container(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{nope\n")
    runner, factory, pool = make()
    with pytest.raises(Exception, match="ledger"):
        runner.open(spec(tmp_path, ledger_path=str(bad), network=("example.com",)))
    assert factory.boxes == [] and pool.users == 0


def test_environment_values_never_reach_the_ledger(tmp_path: Path) -> None:
    runner, _, _ = make()
    handle = runner.open(spec(tmp_path, env={"API_TOKEN": "s3cr3t-value-123"}))
    handle.run(["echo", "hi"], 5, 100)
    handle.close()
    assert "s3cr3t-value-123" not in Path(handle._spec.ledger_path).read_text()


def test_minilocker_missing_means_no_container_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(tmp_path / "nothing"))
    _ml_path.reset_for_tests()
    runner, factory, _ = make()
    with pytest.raises(SandboxUnavailable):
        runner.open(spec(tmp_path))
    assert factory.boxes == []


# -- put -------------------------------------------------------------------------------------------------------------------------


def test_put_runs_a_fixed_script_with_the_path_as_an_argument(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    handle.put("tool/main.py", b"print(1)")
    kind, call = factory.boxes[0].exec_calls[0]
    assert kind == "stdin" and call["data"] == b"print(1)"
    assert call["argv"] == [
        "sh",
        "-c",
        _PUT_SCRIPT,
        "put",
        "/workspace/tool/main.py",
    ]  # the path is $1, never script text
    assert (call["timeout_s"], call["max_out"]) == (60, 4096)


def test_put_refuses_bad_paths_and_data_without_touching_the_box(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    for bad in ("/etc/passwd", "../../etc/x", "/workspace", "", "a\x00b", "/proc/self/x"):
        with pytest.raises(ValueError):
            handle.put(bad, b"x")
    with pytest.raises(ValueError, match="bytes"):
        handle.put("a", "text")  # type: ignore[arg-type]
    assert factory.boxes[0].exec_calls == []


def test_put_failures_say_why_and_never_pass_silently(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    box = factory.boxes[0]
    box.next_stdin = Raw(exit_code=1, stderr=b"No space left\x1b[0m")
    with pytest.raises(SandboxError, match="No space left") as caught:
        handle.put("big", b"x")
    assert "\x1b" not in str(caught.value) and "workspace_mb" in (caught.value.hint or "")
    box.next_stdin = Raw(timed_out=True)
    with pytest.raises(SandboxError, match="timed out"):
        handle.put("slow", b"x")


# -- run -------------------------------------------------------------------------------------------------------------------------


def test_run_forwards_argv_limits_and_returns_the_output(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    factory.boxes[0].next_capped = Raw(exit_code=3, stdout=b"o", stderr=b"e", timed_out=False, truncated=True)
    out = handle.run(["python", "-c", "pass"], 7, 99)
    assert (out.exit_code, out.stdout, out.stderr, out.timed_out, out.truncated) == (
        3,
        b"o",
        b"e",
        False,
        True,
    )
    assert factory.boxes[0].exec_calls[0] == (
        "capped",
        {"argv": ["python", "-c", "pass"], "timeout_s": 7, "max_bytes": 99},
    )


def test_run_validates_its_arguments_before_the_box_sees_them(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    for args in (
        ([], 1, 1),
        (["a"], 0, 1),
        (["a"], 1, -1),
        ("ls", 1, 1),
        (["a\x00"], 1, 1),
        (["a"], True, 1),
    ):
        with pytest.raises(ValueError):
            handle.run(*args)  # type: ignore[arg-type]
    assert factory.boxes[0].exec_calls == []


def test_a_killed_sandbox_refuses_more_work(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    factory.boxes[0].dead = True
    for call in (lambda: handle.run(["a"], 1, 1), lambda: handle.put("a", b""), handle.freeze):
        with pytest.raises(SandboxError, match="killed"):
            call()


def test_a_closed_handle_refuses_more_work(tmp_path: Path) -> None:
    runner, _, _ = make()
    handle = runner.open(spec(tmp_path))
    handle.close()
    with pytest.raises(SandboxError, match="closed"):
        handle.run(["a"], 1, 1)


# -- egress ----------------------------------------------------------------------------------------------------------------------


def test_egress_events_are_logged_once_each_in_order(tmp_path: Path) -> None:
    runner, factory, pool = make()
    handle = runner.open(spec(tmp_path, network=("example.com",)))
    manager = factory.boxes[0].kwargs["egress"]
    manager.events = [{"ts": 1.5, "host": "example.com", "decision": "allowed", "port": 443}]
    handle.run(["curl", "x"], 5, 100)
    manager.events.append({"ts": 2.5, "host": "evil.test", "decision": "blocked", "port": 443})
    handle.run(["curl", "y"], 5, 100)
    summary = handle.close()
    logged = [e for e in handle.events() if e["type"].startswith("egress.")]
    assert [e["type"] for e in logged] == [
        "egress.allowed",
        "egress.blocked",
    ]  # not duplicated by the second run or by close
    assert logged[0]["payload"] == {"proxy_ts": 1.5, "host": "example.com", "port": 443, "source": "code"}
    assert [e["type"] for e in summary.egress_attempts] == ["egress.allowed", "egress.blocked"]
    assert manager.read_args[0][0] == factory.boxes[0].ip and pool.users == 0


def test_a_proxy_that_cannot_be_read_is_recorded_once_not_hidden(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path, network=("example.com",)))
    factory.boxes[0].kwargs["egress"].fail_reads = True
    out = handle.run(["a"], 5, 10)  # the command itself still succeeds
    handle.run(["b"], 5, 10)
    handle.close()
    assert out.exit_code == 0
    assert payloads(handle, "forge.egress.unobserved") == [{"reason": "ConnectionError"}]


def test_network_none_logs_no_egress_events_and_no_unobserved_marker(tmp_path: Path) -> None:
    runner, _, _ = make()
    handle = runner.open(spec(tmp_path))
    handle.run(["a"], 5, 10)
    handle.close()
    assert not [t for t in types(handle) if t.startswith("egress.") or t == "forge.egress.unobserved"]


def test_a_wider_host_list_while_the_proxy_is_busy_is_refused_and_leaks_nothing(tmp_path: Path) -> None:
    runner, factory, pool = make()
    first = runner.open(spec(tmp_path, network=("a.example",), ledger_path=str(tmp_path / "1.jsonl")))
    with pytest.raises(SandboxUnavailable, match="busy"):
        runner.open(spec(tmp_path, network=("b.example",), ledger_path=str(tmp_path / "2.jsonl")))
    assert len(factory.boxes) == 1 and pool.users == 1
    first.close()


# -- decoys ----------------------------------------------------------------------------------------------------------------------


def test_decoys_are_refused_when_the_box_cannot_measure_and_the_box_is_destroyed(tmp_path: Path) -> None:
    runner, factory, pool = make()
    with pytest.raises(SandboxUnavailable, match="decoy"):
        runner.open(spec(tmp_path, decoys=True, network=("example.com",)))
    assert factory.boxes[0].destroyed == 1 and pool.users == 0  # not left running, proxy not held
    events = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]
    destroyed = [e["payload"] for e in events if e["type"] == "sandbox.destroyed"]
    assert [d["reason"] for d in destroyed] == ["decoys_unavailable"]  # recorded once, with the reason


def test_without_decoys_the_watch_stays_inert(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path, decoys=False))
    handle.run(["a"], 5, 10)
    summary = handle.close()
    assert summary.decoy_events == [] and summary.measured == {}


class RecordingWatch:
    """Replaces MiniLocker's Watch to see exactly what the adapter hands it."""

    instances: list[RecordingWatch] = []

    def __init__(self, strict: bool = False, environ: Any = None) -> None:
        self.canaries: Any = None
        self.calls: list[tuple[str, tuple, dict]] = []
        self.audit_error: Exception | None = None
        RecordingWatch.instances.append(self)

    def attach(self, box: Any, log: Any, reseed: bool = False) -> None:
        self.canaries = object()
        self.calls.append(("attach", (), {}))

    def after_tool(self, box: Any, tool: str, result: str, log: Any, probe: bool = False) -> None:
        self.calls.append(("after_tool", (tool, result), {"probe": probe}))

    def on_egress(self, ev: dict, log: Any) -> None:
        self.calls.append(("on_egress", (ev,), {}))

    def audit(self, box: Any, kind: str, log: Any, workspace: bool = True) -> None:
        self.calls.append(("audit", (kind,), {}))
        if self.audit_error is not None:
            raise self.audit_error
        log("sandbox", "sandbox.audit", {"kind": kind, "canaries": {"files": {}}})


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> type[RecordingWatch]:
    RecordingWatch.instances = []
    monkeypatch.setattr(_ml_path.load("minilocker.agent.watch"), "Watch", RecordingWatch)
    return RecordingWatch


def test_after_tool_gets_the_full_text_and_probes(recording: type[RecordingWatch], tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path, decoys=True))
    factory.boxes[0].next_capped = Raw(stdout=b"x" * 5000 + b"TAIL", stderr=b"err")
    handle.run(["cat", "f"], 5, 10_000)
    (call,) = [c for c in recording.instances[0].calls if c[0] == "after_tool"]
    assert call[1][0] == "run" and call[2] == {"probe": True}
    assert call[1][1].startswith("x" * 5000 + "TAIL") and call[1][1].endswith(
        "err"
    )  # nothing cut off before the search


def test_after_tool_is_not_called_without_decoys(recording: type[RecordingWatch], tmp_path: Path) -> None:
    runner, _, _ = make()
    handle = runner.open(spec(tmp_path, decoys=False))
    handle.run(["a"], 5, 10)
    assert not [c for c in recording.instances[0].calls if c[0] == "after_tool"]


def test_close_reports_what_the_audit_measured(recording: type[RecordingWatch], tmp_path: Path) -> None:
    runner, _, _ = make()
    handle = runner.open(spec(tmp_path, decoys=True))
    summary = handle.close()
    assert summary.measured == {"kind": "code", "canaries": {"files": {}}} and summary.destroyed is True
    assert summary.hardening == {"network_mode": "none", "read_only_rootfs": True}


# -- close -----------------------------------------------------------------------------------------------------------------------


def test_close_destroys_the_container_even_if_the_audit_raises(
    recording: type[RecordingWatch], tmp_path: Path
) -> None:
    runner, factory, pool = make()
    handle = runner.open(spec(tmp_path, decoys=True, network=("example.com",)))
    recording.instances[0].audit_error = RuntimeError("audit broke")
    with pytest.raises(RuntimeError, match="audit broke"):
        handle.close()
    assert factory.boxes[0].destroyed == 1 and pool.users == 0
    assert "sandbox.destroyed" in types(handle)


def test_close_is_idempotent(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    first = handle.close()
    assert handle.close() is first and factory.boxes[0].destroyed == 1
    assert types(handle).count("sandbox.destroyed") == 1


def test_leaving_a_with_block_closes_also_after_an_error(tmp_path: Path) -> None:
    runner, factory, pool = make()
    with pytest.raises(KeyError):
        with runner.open(spec(tmp_path, network=("example.com",))):
            raise KeyError("boom")
    assert factory.boxes[0].destroyed == 1 and pool.users == 0


def test_a_container_that_is_already_gone_counts_as_destroyed(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    factory.boxes[0].destroy_error = NotFound("gone")
    assert handle.close().destroyed is True


def test_a_container_that_cannot_be_removed_is_an_error_not_a_success(tmp_path: Path) -> None:
    runner, factory, pool = make()
    handle = runner.open(spec(tmp_path, network=("example.com",)))
    factory.boxes[0].destroy_error = PermissionError("denied")
    with pytest.raises(SandboxError, match="could not remove") as caught:
        handle.close()
    assert "docker rm -f" in (caught.value.hint or "") and pool.users == 0  # the proxy is released regardless
    assert "sandbox.destroyed" not in types(handle)  # never claim a destroy that did not happen
    assert handle.close().destroyed is False


def test_the_destroyed_event_carries_the_label(tmp_path: Path) -> None:
    runner, _, _ = make()
    handle = runner.open(spec(tmp_path, label="gate:G3"))
    handle.close()
    (destroyed,) = payloads(handle, "sandbox.destroyed")
    assert destroyed == {"name": handle.name, "reason": "close", "label": "gate:G3"}


def test_the_summary_only_counts_this_handles_events_on_a_resumed_ledger(tmp_path: Path) -> None:
    runner, factory, _ = make()
    first = runner.open(spec(tmp_path, network=("example.com",)))
    factory.boxes[0].kwargs["egress"].events = [{"ts": 1.0, "host": "example.com", "decision": "allowed"}]
    first.run(["a"], 5, 10)
    first.close()
    second = runner.open(spec(tmp_path))  # the same file
    assert second.close().egress_attempts == []


# -- freeze and thaw -------------------------------------------------------------------------------------------------------------


def test_freeze_returns_the_archive_bytes(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    factory.boxes[0].next_capped = Raw(stdout=b"\x1f\x8bARCHIVE")
    assert handle.freeze() == b"\x1f\x8bARCHIVE"
    assert factory.boxes[0].exec_calls[0][1]["argv"][:3] == ["tar", "-C", "/workspace"]


def test_freeze_errors_are_sandbox_errors(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    box = factory.boxes[0]
    for raw, text in (
        (Raw(exit_code=2), "tar exit 2"),
        (Raw(timed_out=True), "timed out"),
        (Raw(truncated=True), "too large"),
    ):
        box.next_capped = raw
        with pytest.raises(SandboxError, match=text):
            handle.freeze()


def test_thaw_restores_and_checks_its_input_and_result(tmp_path: Path) -> None:
    runner, factory, _ = make()
    handle = runner.open(spec(tmp_path))
    box = factory.boxes[0]
    handle.thaw(b"blob")
    assert box.exec_calls[0][1]["data"] == b"blob"
    for bad in (b"", "text", None):
        with pytest.raises(SandboxError, match="freeze returned"):
            handle.thaw(bad)  # type: ignore[arg-type]
    box.next_stdin = Raw(exit_code=2, stderr=b"not gzip")
    with pytest.raises(SandboxError, match="could not restore"):
        handle.thaw(b"junk")
    box.next_stdin = Raw(timed_out=True)
    with pytest.raises(SandboxError, match="timed out"):
        handle.thaw(b"slow")


# -- concurrency -----------------------------------------------------------------------------------------------------------------


def test_concurrent_opens_get_their_own_boxes_and_the_pool_count_balances(tmp_path: Path) -> None:
    runner, factory, pool = make()
    handles: list[Any] = []
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            handle = runner.open(
                spec(tmp_path, network=("example.com",), ledger_path=str(tmp_path / f"{n}.jsonl"))
            )
            handles.append(handle)
        except BaseException as exc:  # pragma: no cover - would fail the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len({h.name for h in handles}) == 8 and pool.users == 8
    for handle in handles:
        handle.close()
    assert pool.users == 0 and all(box.destroyed == 1 for box in factory.boxes)
    assert runner.shutdown() is True


def test_shutdown_is_refused_while_a_sandbox_is_open(tmp_path: Path) -> None:
    runner, _, pool = make()
    handle = runner.open(spec(tmp_path, network=("example.com",)))
    assert runner.shutdown() is False
    handle.close()
    assert runner.shutdown() is True and pool.allowed == frozenset()
