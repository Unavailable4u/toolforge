"""The stand-in sandbox of the demo refuses anything that carries the hostile marker, so it cannot run the hostile fixtures."""

from __future__ import annotations

import pytest

from demo import hostile
from demo.standin import guarded_runner
from toolforge.errors import SandboxError
from toolforge.sandbox.base import SandboxSpec


def test_a_box_that_holds_the_marker_is_refused_before_anything_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = guarded_runner()
    try:
        handle = runner.open(SandboxSpec(image="x", network="none"))
        ran: list[list[str]] = []
        parent = type(handle).__mro__[1]
        monkeypatch.setattr(parent, "run", lambda self, argv, timeout_s, max_out: ran.append(argv) or "ran")
        handle.put("/workspace/tool.py", b"print('fine')")
        assert handle.run(["python", "x"], 5, 100) == "ran" and len(ran) == 1
        handle.put("/workspace/sub/evil.py", f"# {hostile.MARKER}\n".encode())
        with pytest.raises(SandboxError, match="no isolation"):
            handle.run(["python", "x"], 5, 100)
        assert len(ran) == 1, "the refused run must not reach the host Python"
    finally:
        runner.cleanup()


def test_the_marker_is_in_both_hostile_packages_and_in_no_honest_one() -> None:
    for name in hostile.NAMES:
        files = hostile.payload(name)["files"]
        assert any(hostile.MARKER in text for text in files.values()), name
    assert not hostile.carries_marker(b"an honest tool")
    assert hostile.carries_marker(f"x {hostile.MARKER} y")
