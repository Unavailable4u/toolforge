"""FakeRunner: records every spec, applies the real argument rules, never runs code, freezes deterministically."""

from __future__ import annotations

import ast
import dataclasses
import gzip
import io
import tarfile
import threading
from pathlib import Path

import pytest

from toolforge.errors import SandboxError, SandboxUnavailable
from toolforge.sandbox import FakeRunner, RunOutput, SandboxSpec
from toolforge.sandbox.base import normalize_path

SRC = Path(__file__).resolve().parent.parent / "src" / "toolforge" / "sandbox"


def ok(stdout: bytes = b"", code: int = 0) -> RunOutput:
    return RunOutput(code, stdout, b"", False, False)


def spec(**kwargs: object) -> SandboxSpec:
    return SandboxSpec(**{"image": "python:3.12-slim", "network": "none", **kwargs})  # type: ignore[arg-type]


# -- the spec -----------------------------------------------------------------------------------------------------------


def test_spec_defaults_and_frozen() -> None:
    s = spec()
    assert (s.workspace_mb, s.memory_mb, s.pids, s.decoys, s.ledger_path, s.label) == (
        64,
        256,
        128,
        False,
        None,
        "",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.image = "other"  # type: ignore[misc]


def test_spec_normalizes_hosts_and_keeps_a_private_env_copy() -> None:
    env = {"A": "1"}
    s = spec(network=("API.Example.com", "api.example.com", " b.example.com "), env=env)
    assert s.network == ("api.example.com", "b.example.com")
    env["A"] = "changed"
    assert s.env == {"A": "1"}


@pytest.mark.parametrize(
    "bad",
    [
        (),
        "api.example.com",  # a bare string is not a tuple of hosts
        ("https://api.example.com",),
        ("api.example.com:443",),
        ("*.example.com",),
        ("a b",),
        ("a/b",),
        ("",),
    ],
)
def test_spec_rejects_bad_network(bad: object) -> None:
    with pytest.raises(ValueError, match="network"):
        spec(network=bad)


@pytest.mark.parametrize("field", ["workspace_mb", "memory_mb", "pids"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8"])
def test_spec_rejects_bad_limits(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        spec(**{field: value})


def test_spec_rejects_empty_image_and_non_string_env() -> None:
    with pytest.raises(ValueError, match="image"):
        spec(image="  ")
    with pytest.raises(ValueError, match="env"):
        spec(env={"A": 1})


# -- recording and scripting --------------------------------------------------------------------------------------------


def test_every_open_is_recorded_with_what_production_asked_for() -> None:
    runner = FakeRunner([])
    wanted = spec(
        network=("api.example.com",), memory_mb=512, pids=64, decoys=True, label="verify:x@1.0.0:G3"
    )
    with runner.open(spec()):
        pass
    with runner.open(wanted):
        pass
    assert runner.opened[1] is wanted and runner.opened[0].network == "none"
    assert (runner.opened[1].memory_mb, runner.opened[1].pids, runner.opened[1].decoys) == (512, 64, True)


def test_an_unavailable_runner_refuses_but_still_records() -> None:
    runner = FakeRunner(available=False)
    with pytest.raises(SandboxUnavailable):
        runner.open(spec())
    assert len(runner.opened) == 1 and runner.handles == []


def test_open_takes_only_a_spec() -> None:
    with pytest.raises(TypeError):
        FakeRunner([]).open({"image": "x"})  # type: ignore[arg-type]


def test_a_function_expectation_sees_argv_and_the_files_that_were_put() -> None:
    seen: list[tuple[list[str], dict[str, bytes]]] = []

    def responder(argv: list[str], files: dict[str, bytes]) -> RunOutput:
        seen.append((argv, files))
        return ok(files["/workspace/in.txt"].upper())

    with FakeRunner(responder).open(spec()) as box:
        box.put("in.txt", b"abc")
        out = box.run(["cat", "in.txt"], 5, 1000)
    assert out.stdout == b"ABC" and seen == [(["cat", "in.txt"], {"/workspace/in.txt": b"abc"})]


def test_canned_outputs_are_consumed_in_order_per_handle_and_then_fail_loudly() -> None:
    runner = FakeRunner([ok(b"one"), ok(b"two")])
    with runner.open(spec()) as a:
        assert [a.run(["x"], 5, 10).stdout, a.run(["x"], 5, 10).stdout] == [b"one", b"two"]
        with pytest.raises(AssertionError, match="no expectation"):
            a.run(["x"], 5, 10)
    with runner.open(spec()) as b:  # a second handle starts from the full list again
        assert b.run(["x"], 5, 10).stdout == b"one"


def test_no_expectation_is_an_error_not_an_invented_result() -> None:
    with FakeRunner().open(spec()) as box, pytest.raises(AssertionError, match="no expectation"):
        box.run(["true"], 5, 10)


def test_expect_replaces_the_script_for_later_handles() -> None:
    runner = FakeRunner([ok(b"old")])
    runner.expect([ok(b"new")])
    with runner.open(spec()) as box:
        assert box.run(["x"], 5, 10).stdout == b"new"


def test_a_responder_must_return_run_output() -> None:
    with (
        FakeRunner(lambda argv, files: "text").open(spec()) as box,
        pytest.raises(AssertionError, match="RunOutput"),
    ):  # type: ignore[arg-type,return-value]
        box.run(["x"], 5, 10)


def test_run_calls_are_recorded_for_assertions() -> None:
    with FakeRunner([ok()]).open(spec()) as box:
        box.run(["python", "main.py"], 7, 99)
        assert [(c.argv, c.timeout_s, c.max_out) for c in box.calls] == [(["python", "main.py"], 7, 99)]


# -- argument rules (the same ones the real runner applies) -----------------------------------------------------------------


@pytest.mark.parametrize("argv", ["ls -la", [], ["ok", 1], ["a\x00b"], None, ("sh", "-c", "x", b"y")])
def test_run_rejects_bad_argv(argv: object) -> None:
    with FakeRunner([ok()]).open(spec()) as box, pytest.raises(ValueError, match="argv"):
        box.run(argv, 5, 10)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -3, True, 2.5, None, "5"])
def test_run_rejects_bad_timeout_and_cap(value: object) -> None:
    with FakeRunner([ok(), ok()]).open(spec()) as box:
        with pytest.raises(ValueError, match="timeout_s"):
            box.run(["x"], value, 10)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_out"):
            box.run(["x"], 5, value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("main.py", "/workspace/main.py"),
        ("a/b/../c.txt", "/workspace/a/c.txt"),
        ("/workspace/x", "/workspace/x"),
        ("/tmp/scratch", "/tmp/scratch"),
        ("./d/./e", "/workspace/d/e"),
    ],
)
def test_put_paths_resolve_under_the_writable_roots(given: str, expected: str) -> None:
    assert normalize_path(given) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../escape",
        "a/../../etc/x",
        "/workspace",
        "/tmp",
        "/workspacefoo/x",
        "",
        "a\x00b",
        "/tmp/../etc/x",
    ],
)
def test_put_refuses_everything_outside_workspace_and_tmp(bad: str) -> None:
    with FakeRunner([]).open(spec()) as box, pytest.raises(ValueError):
        box.put(bad, b"x")
    assert box.files == {}


def test_put_takes_bytes_only() -> None:
    with FakeRunner([]).open(spec()) as box, pytest.raises(ValueError, match="bytes"):
        box.put("a", "text")  # type: ignore[arg-type]


# -- output limits and timeouts -----------------------------------------------------------------------------------------------


def test_stdout_is_capped_like_the_real_runner() -> None:
    with FakeRunner([ok(b"0123456789")]).open(spec()) as box:
        out = box.run(["x"], 5, 4)
    assert out.stdout == b"0123" and out.truncated is True


def test_a_timeout_kills_the_handle() -> None:
    timed_out = RunOutput(137, b"", b"", True, False)
    with FakeRunner([timed_out, ok()]).open(spec()) as box:
        assert box.run(["sleep", "99"], 1, 10).timed_out is True
        with pytest.raises(SandboxError, match="killed"):
            box.run(["true"], 5, 10)
        with pytest.raises(SandboxError):
            box.put("a", b"x")


# -- freeze and thaw ------------------------------------------------------------------------------------------------------------


def test_freeze_is_deterministic_whatever_the_put_order() -> None:
    with FakeRunner([]).open(spec()) as a, FakeRunner([]).open(spec()) as b:
        a.put("one.txt", b"1")
        a.put("dir/two.txt", b"2")
        b.put("dir/two.txt", b"2")
        b.put("one.txt", b"1")
        assert a.freeze() == b.freeze()
        b.put("one.txt", b"changed")
        assert a.freeze() != b.freeze()


def test_freeze_holds_the_workspace_and_not_tmp() -> None:
    with FakeRunner([]).open(spec()) as box:
        box.put("keep.txt", b"keep")
        box.put("/tmp/scratch", b"scratch")
        blob = box.freeze()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        assert tar.getnames() == ["./keep.txt"]
        member = tar.getmember("./keep.txt")
        assert (member.mtime, member.uid, member.gid, member.mode) == (0, 0, 0, 0o644)


def test_thaw_round_trips_into_another_sandbox() -> None:
    runner = FakeRunner([])
    with runner.open(spec()) as first:
        first.put("data/in.csv", b"a,b\n1,2\n")
        blob = first.freeze()
    with runner.open(spec()) as second:
        second.thaw(blob)
        assert second.files == {"/workspace/data/in.csv": b"a,b\n1,2\n"}
        assert second.freeze() == blob


def _tar(*members: tarfile.TarInfo, payload: bytes = b"x") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for member in members:
            tar.addfile(member, io.BytesIO(payload) if member.isreg() else None)
    return gzip.compress(raw.getvalue())


def _member(name: str, kind: bytes = tarfile.REGTYPE, size: int = 1) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size = kind, size if kind == tarfile.REGTYPE else 0
    if kind == tarfile.SYMTYPE:
        info.linkname = "/etc/passwd"
    return info


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"not a tar",
        gzip.compress(b"not a tar either"),
        _tar(_member("/abs/path")),
        _tar(_member("../escape")),
        _tar(_member("link", tarfile.SYMTYPE)),
        _tar(_member("fifo", tarfile.FIFOTYPE)),
    ],
)
def test_thaw_refuses_what_freeze_could_not_have_made(blob: bytes) -> None:
    with FakeRunner([]).open(spec()) as box, pytest.raises(SandboxError):
        box.thaw(blob)
    assert box.files == {}


# -- handles as context managers and the audit ----------------------------------------------------------------------------------


def test_leaving_the_block_closes_the_sandbox_even_after_an_exception() -> None:
    runner = FakeRunner([])
    with pytest.raises(RuntimeError), runner.open(spec()) as box:
        raise RuntimeError("boom")
    assert box.closed is True
    with pytest.raises(SandboxError, match="closed"):
        box.put("a", b"x")


def test_close_is_idempotent_and_reports_destroyed() -> None:
    box = FakeRunner([]).open(spec(ledger_path="/nowhere/ledger.jsonl"))
    first = box.close()
    assert box.close() is first and first.destroyed is True
    assert first.ledger_path == "/nowhere/ledger.jsonl"


def test_a_fake_measured_nothing_and_says_so() -> None:
    summary = FakeRunner([]).open(spec()).close()
    assert summary.hardening == {} and summary.measured == {}  # "not measured", never a made-up all-clear
    assert summary.decoy_events == [] and summary.egress_attempts == []


def test_a_test_can_supply_what_it_wants_the_audit_to_say() -> None:
    runner = FakeRunner([], hardening={"network_mode": "none"}, measured={"incomplete": ["cpu"]})
    summary = runner.open(spec()).close()
    assert summary.hardening == {"network_mode": "none"} and summary.measured == {"incomplete": ["cpu"]}


def test_simulated_events_are_split_into_decoy_and_egress_lists() -> None:
    box = FakeRunner([]).open(spec())
    box.add_event("canary.read", {"item": "aws_secret"})
    box.add_event("egress.blocked", {"host": "evil.example"})
    box.add_event("sandbox.created", {})
    summary = box.close()
    assert [e["type"] for e in summary.decoy_events] == ["canary.read"]
    assert [e["type"] for e in summary.egress_attempts] == ["egress.blocked"]
    assert [e["type"] for e in box.events()] == ["canary.read", "egress.blocked", "sandbox.created"]


def test_events_returns_copies() -> None:
    box = FakeRunner([]).open(spec())
    box.add_event("canary.read", {"item": "x"})
    box.events()[0]["payload"]["item"] = "tampered"
    assert box.events()[0]["payload"]["item"] == "x"


def test_the_runner_can_be_used_from_several_threads() -> None:
    runner = FakeRunner([])
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(25):
                with runner.open(spec()):
                    pass
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(runner.opened) == 200 and len(runner.handles) == 200


# -- honesty -------------------------------------------------------------------------------------------------------------------


def test_the_fake_and_the_seam_import_nothing_that_could_run_code() -> None:
    """A fake that can execute things is not a fake. Neither module may import a way to start a process or reach Docker."""
    forbidden = {
        "subprocess",
        "os",
        "docker",
        "socket",
        "multiprocessing",
        "ctypes",
        "pty",
        "shlex",
        "minilocker",
        "importlib",
    }
    for name in ("fake.py", "base.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
        }
        assert roots & forbidden == set(), f"{name} imports {roots & forbidden}"


def test_a_scripted_command_does_not_touch_the_host(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    with FakeRunner([ok()]).open(spec()) as box:
        box.run(["sh", "-c", f"touch {marker}"], 5, 10)
    assert not marker.exists()
