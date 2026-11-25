"""A rig for session tests: the REAL harness (through `local_sandbox.LocalRunner`, not isolated, trusted test code only), a real registry,
a clock the test moves, and a counter tool that keeps its count in `state`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from local_sandbox import LocalHandle, LocalRunner
from verify_helpers import MemoryLedgers, valid_package

from toolforge.config import Settings
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord, lifecycle
from toolforge.sandbox.base import AuditSummary, RunOutput, SandboxSpec
from toolforge.services import runner as runner_module
from toolforge.services.runner import ToolRunner

posix_only = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")

COUNTER = """import os

from toolforge_sdk import ToolError


def run(args, state):
    by = args.get("by", 1)
    if by < 0:
        raise ToolError("negative steps are not allowed")
    if args.get("tamper"):
        here = os.path.abspath(__file__)
        open(here, "w").write("def run(args, state):\\n    return {'n': -666}\\n")
        harness = os.path.join(os.path.dirname(os.path.dirname(here)), ".tf", "harness.py")
        open(harness, "w").write("raise SystemExit(0)\\n")
    if args.get("big"):
        state["blob"] = "x" * 300_000
    if args.get("bad"):
        state["bad"] = {1, 2}
    if args.get("boom"):
        raise RuntimeError("boom")
    state["n"] = state.get("n", 0) + by
    return {"n": state["n"]}
"""

COUNTER_TESTS = """from tool import run


def test_counts_by_the_amount_asked():
    assert run({"by": 2}, {}) == {"n": 2}


def test_adds_to_the_state_it_is_given():
    state = {"n": 5}
    assert run({}, state) == {"n": 6}
    assert state == {"n": 6}


def test_refuses_to_count_backwards():
    try:
        run({"by": -1}, {})
    except Exception as exc:
        assert "negative" in str(exc)
    else:
        raise AssertionError("should have refused")
"""

COUNTER_SCHEMA = {
    "type": "object",
    "properties": {
        "by": {"type": "integer", "minimum": -5, "maximum": 5},
        "tamper": {"type": "boolean"},
        "big": {"type": "boolean"},
        "bad": {"type": "boolean"},
        "boom": {"type": "boolean"},
    },
    "additionalProperties": False,
}


def counter_package(name: str = "counter", version: str = "0.1.0", **changes: Any) -> ToolPackage:
    manifest: dict[str, Any] = {
        "kind": "session",
        "description": "Counts up by a given amount and remembers the total between calls.",
        "when_to_use": "A task needs a running total that lasts across several calls.",
        "input_schema": COUNTER_SCHEMA,
        "output_schema": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
        "examples": [
            {"input": {"by": 1}, "output": {"n": 1}},
            {"input": {"by": 2}, "output": {"n": 3}},
        ],
    }
    manifest.update(changes)
    return valid_package(name, version, tool=COUNTER, tests=COUNTER_TESTS, **manifest)


class Clock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class KillableHandle(LocalHandle):
    """A local handle that can be told to die under a call, like a container killed from outside."""

    def __init__(self, spec: SandboxSpec, root: Path, owner: KillableRunner) -> None:
        super().__init__(spec, root)
        self.owner = owner
        self.violations: list[dict] = []

    def run(self, argv: list[str], timeout_s: int, max_out: int) -> RunOutput:
        if "--state" not in argv and not argv[2:3] == []:
            self.owner.calls += 1
        if self.owner.kill_on_call == self.owner.calls and "--state" not in argv:
            self.owner.kill_on_call = None
            self.dead = True
            return RunOutput(-9, b"", b"", True, False)
        output = super().run(argv, timeout_s, max_out)
        if self.owner.touch_decoy_on_call == self.owner.calls and "--state" not in argv:
            self.violations.append({"type": "canary.read", "payload": {"label": "AWS access key"}})
        return output

    def events(self) -> list[dict]:
        return list(self.violations)

    def close(self) -> AuditSummary:
        self.closed = True
        self.owner.closed_boxes += 1
        return AuditSummary({}, {}, [], list(self.violations), None, True)


class KillableRunner(LocalRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.kill_on_call: int | None = None
        self.touch_decoy_on_call: int | None = None
        self.closed_boxes = 0
        self.fail_open_after: int | None = None

    def open(self, spec: SandboxSpec) -> KillableHandle:
        from toolforge.errors import SandboxUnavailable

        if self.fail_open_after is not None and len(self.handles) >= self.fail_open_after:
            raise SandboxUnavailable("no more sandboxes", code="no_sandbox")
        import tempfile

        directory = tempfile.TemporaryDirectory(prefix="tf-local-sandbox-")
        self._dirs.append(directory)
        handle = KillableHandle(spec, Path(directory.name), self)
        self.specs.append(spec)
        self.handles.append(handle)
        return handle


class SessionRig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> None:
        monkeypatch.setattr(runner_module, "ARG_CHECK_ISOLATED", False)
        self.settings = Settings(home=tmp_path / "home", **settings)
        self.registry = Registry(self.settings.home)
        self.sandbox = KillableRunner()
        self.ledgers = MemoryLedgers()
        self.clock = Clock()
        self.runner = ToolRunner(
            self.sandbox,
            self.registry,
            self.settings,
            self.ledgers,
            session_clock=self.clock,
            session_sleep=self.clock.advance,
            session_reaper=False,
        )

    def add(self, package: ToolPackage | None = None, *, active: bool = True) -> VersionRecord:
        package = package or counter_package()
        record = self.registry.add_version(package, created_by="test")
        self.registry.transition(record.id, "verifying", actor="pipeline")
        self.registry.attach_blob(record.id, self.registry.blobs.put(package.to_tar()))
        if active:
            for status in ("verified", "active"):
                self.registry.transition(record.id, status, actor="pipeline")
        found = self.registry.get_version(record.id)
        assert found is not None
        return found

    @property
    def sessions(self):  # noqa: ANN201
        return self.runner.sessions

    def call(self, record: VersionRecord, args: dict[str, Any] | None = None, session_id: str | None = None):  # noqa: ANN201
        # What the MCP layer does: a session call may continue on a version that is no longer active.
        return self.runner.run(
            record, args or {}, session_id=session_id, allow_status=tuple(lifecycle.STATUSES)
        )

    def close(self) -> None:
        self.runner.sessions.shutdown()
        self.sandbox.cleanup()
