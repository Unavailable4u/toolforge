"""The development sandbox for the demo: `tests/local_sandbox.py` with a guard. NOT a sandbox.

`--sandbox stand-in` lets the demo run on a machine with no Docker (CI, a laptop without the daemon). It runs the real harness with the
host's own Python and gives **no isolation at all**, so it is for code that is checked in. The guard makes that a rule instead of a
hope: before anything runs, every file in the box is searched for `hostile.MARKER`, and a box that carries it is refused. The hostile
fixtures of act 3 carry it, and gate G1 rejects the first of them before any box is opened anyway; the guard is the second line.

Recordings made with it say `sandbox: stand-in` and `isolated: false`. Proven by `tests/test_demo_standin.py`.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from demo.hostile import carries_marker
from toolforge.errors import SandboxError

TESTS = Path(__file__).resolve().parent.parent / "tests"


class StandInUnavailable(RuntimeError):
    pass


def load_local_sandbox() -> ModuleType:
    if not (TESTS / "local_sandbox.py").is_file():
        raise StandInUnavailable(
            "--sandbox stand-in needs tests/local_sandbox.py, so it only works from a full checkout"
        )
    if str(TESTS) not in sys.path:
        sys.path.insert(0, str(TESTS))
    import local_sandbox

    return local_sandbox


def guarded_runner() -> Any:
    module = load_local_sandbox()

    class GuardedHandle(module.LocalHandle):
        def run(self, argv, timeout_s, max_out):
            for path in self.root.rglob("*"):
                if path.is_file() and carries_marker(path.read_bytes()):
                    raise SandboxError(
                        "the stand-in sandbox refuses to run a hostile fixture: it has no isolation",
                        hint="run the demo with --sandbox docker",
                    )
            return super().run(argv, timeout_s, max_out)

    class GuardedRunner(module.LocalRunner):
        def open(self, spec):
            directory = tempfile.TemporaryDirectory(prefix="tf-demo-stand-in-")
            self._dirs.append(directory)
            handle = GuardedHandle(spec, Path(directory.name))
            self.specs.append(spec)
            self.handles.append(handle)
            return handle

    return GuardedRunner()
