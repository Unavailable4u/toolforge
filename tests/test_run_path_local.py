"""`ToolRunner` and the real harness together, without Docker: a stand-in sandbox that runs the harness in a temporary folder.

This proves that the host side and the in-sandbox side agree on the layout (`pkg/`, `inputs/`, `.tf/`), the command, the envelope
and the failure mapping, and that both reference packs still return exactly their manifest examples. The stand-in is NOT a
sandbox and gives no isolation; it runs only our own checked-in code (`examples/toolpacks`, `tests/fixtures/trusted_tools`), never a
generated or submitted package. The real containers are covered by `tests/docker/test_run_path.py`.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from toolforge.config import Settings
from toolforge.models.package import ToolPackage
from toolforge.sandbox.base import AuditSummary, RunOutput, SandboxSpec, normalize_path
from toolforge.services import runner as runner_module
from toolforge.services.runner import ToolRunner

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container")

ROOT = Path(__file__).resolve().parent.parent
PACKS = ROOT / "examples" / "toolpacks"


class LocalHandle:
    """Maps /workspace to a temporary folder and runs the harness with the host's own Python. Trusted code only."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def _real(self, path: str) -> Path:
        normal = normalize_path(path)
        assert normal.startswith("/workspace/"), normal
        return self.workspace / normal[len("/workspace/") :]

    def put(self, path: str, data: bytes) -> None:
        target = self._real(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def thaw(self, blob: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            for member in archive.getmembers():
                assert member.isreg() and not member.name.startswith(("/", ".."))
                self.put(f"/workspace/{member.name}", archive.extractfile(member).read())  # type: ignore[union-attr]

    def run(self, argv: list[str], timeout_s: int, max_out: int) -> RunOutput:
        assert argv[:2] == ["python", "/workspace/.tf/harness.py"], argv
        proc = subprocess.run(
            [sys.executable, str(self._real(argv[1])), argv[2]],
            capture_output=True,
            timeout=timeout_s,
            env={"PATH": os.environ.get("PATH", "")},
            check=False,
        )
        return RunOutput(
            proc.returncode, proc.stdout[:max_out], proc.stderr, False, len(proc.stdout) > max_out
        )

    def freeze(self) -> bytes:  # pragma: no cover - not used by a call
        raise NotImplementedError

    def events(self) -> list[dict]:
        return []

    def close(self) -> AuditSummary:
        return AuditSummary({}, {}, [], [], None, True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


class LocalRunner:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.count = 0

    def open(self, spec: SandboxSpec) -> LocalHandle:
        self.count += 1
        workspace = self.base / f"box{self.count}"
        workspace.mkdir()
        return LocalHandle(workspace)


class Ledger:
    def append(self, actor: str, type: str, payload: dict | None = None) -> dict:
        return {}


@pytest.fixture
def tool_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolRunner:
    monkeypatch.setattr(runner_module, "ARG_CHECK_ISOLATED", False)
    return ToolRunner(
        LocalRunner(tmp_path), None, Settings(home=tmp_path / "home"), lambda path, task: Ledger()
    )


def pack(name: str) -> ToolPackage:
    return ToolPackage.from_dir(PACKS / name)


@pytest.mark.parametrize("name", ["word_count", "csv_summary"])
def test_a_reference_pack_returns_exactly_its_manifest_example(tool_runner: ToolRunner, name: str) -> None:
    package = pack(name)
    assert package.manifest.examples
    for example in package.manifest.examples:
        files = {path: text.encode() for path, text in (example.files or {}).items()}
        result = tool_runner.run_unverified(package, example.input, files=files)
        assert result.ok, result.error
        assert result.result == example.output
        assert result.error is None and result.truncated is False


def test_a_relative_input_path_resolves_inside_the_workspace(tool_runner: ToolRunner) -> None:
    """The decision P05 had to make: the tool's working directory is /workspace, so `inputs/data.csv` just works."""
    result = tool_runner.run_unverified(
        pack("csv_summary"), {"path": "inputs/data.csv"}, files={"inputs/data.csv": b"a\n1\n3\n"}
    )
    assert result.ok and result.result["rows"] == 2 and result.result["numeric"]["a"]["mean"] == 2.0


def test_a_missing_input_file_is_a_tool_error_the_caller_can_act_on(tool_runner: ToolRunner) -> None:
    result = tool_runner.run_unverified(
        pack("csv_summary"), {"path": "inputs/other.csv"}, files={"inputs/data.csv": b"a\n1\n"}
    )
    assert not result.ok and result.error is not None and result.error.type == "ToolError"
    assert "cannot read" in result.error.message


def test_an_empty_csv_is_a_tool_error(tool_runner: ToolRunner) -> None:
    result = tool_runner.run_unverified(
        pack("csv_summary"), {"path": "inputs/data.csv"}, files={"inputs/data.csv": b""}
    )
    assert result.error is not None and result.error.type == "ToolError" and "empty" in result.error.message


def test_a_tool_that_raises_is_a_crash_with_a_clean_trace(tool_runner: ToolRunner) -> None:
    payload = {
        "manifest": json.loads((PACKS / "word_count" / "manifest.json").read_text(encoding="utf-8")),
        "files": {
            "tool.py": "def run(args):\n    return {'words': args['missing']}\n",
            "tests/test_tool.py": "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n\ndef test_c():\n    assert True\n",
        },
    }
    package = ToolPackage.from_submission(payload)
    result = tool_runner.run_unverified(package, {"text": "a b"})
    assert not result.ok and result.error is not None and result.error.type == "Crash"
    assert "KeyError" in result.error.message and "tool.py" in result.error.trace_tail
