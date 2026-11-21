"""A stand-in `SandboxRunner` that runs the real harness in a temporary folder with the host's own Python.

NOT a sandbox: it gives no isolation at all. It exists so the verification gates can be proven end to end against the real harness
without Docker, and it must only ever run our own checked-in code or code that a test in this repository wrote itself (the reference
packs, `tests/fixtures/trusted_tools`, the small packages the tests build). Never point it at a submitted or generated package.

It understands exactly the commands the gates send (`python /workspace/.tf/harness.py <nonce> [flags]`); anything else, such as
`pip`, raises, so a test cannot silently depend on something this stand-in does not do. The real containers are covered by
`tests/docker/`.
"""

from __future__ import annotations

import gzip
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Self

from toolforge.sandbox.base import AuditSummary, RunOutput, SandboxSpec, normalize_path


class LocalHandle:
    def __init__(self, spec: SandboxSpec, root: Path) -> None:
        self.spec = spec
        self.root = root
        self.closed = False
        self.dead = False
        self.runs: list[list[str]] = []

    def _real(self, path: str) -> Path:
        normal = normalize_path(path)
        assert normal.startswith("/workspace/"), normal
        return self.root / normal[len("/workspace/") :]

    def put(self, path: str, data: bytes) -> None:
        target = self._real(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def thaw(self, blob: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                assert member.isreg() and not member.name.startswith(("/", "..")), member.name
                self.put(f"/workspace/{member.name}", archive.extractfile(member).read())  # type: ignore[union-attr]

    def run(self, argv: list[str], timeout_s: int, max_out: int) -> RunOutput:
        assert not self.dead, "the sandbox was stopped by an earlier timeout"
        assert argv[:2] == ["python", "/workspace/.tf/harness.py"], (
            f"the stand-in only runs the harness, not {argv}"
        )
        self.runs.append(list(argv))
        try:
            proc = subprocess.run(
                [sys.executable, str(self._real(argv[1])), *argv[2:]],
                capture_output=True,
                timeout=timeout_s,
                env={"PATH": os.environ.get("PATH", "")},
                cwd=self.root,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            self.dead = True
            return RunOutput(-9, (expired.stdout or b"")[:max_out], expired.stderr or b"", True, False)
        return RunOutput(
            proc.returncode, proc.stdout[:max_out], proc.stderr, False, len(proc.stdout) > max_out
        )

    def freeze(self) -> bytes:
        buffer = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
                    data = path.read_bytes()
                    info = tarfile.TarInfo("./" + path.relative_to(self.root).as_posix())
                    info.size, info.mtime, info.mode = len(data), 0, 0o644
                    tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def events(self) -> list[dict]:
        return []

    def close(self) -> AuditSummary:
        self.closed = True
        return AuditSummary({}, {}, [], [], None, True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


class LocalRunner:
    """Opens `LocalHandle`s, each in its own temporary folder. Records every spec it was asked for."""

    def __init__(self) -> None:
        self.specs: list[SandboxSpec] = []
        self.handles: list[LocalHandle] = []
        self._dirs: list[tempfile.TemporaryDirectory[str]] = []

    def open(self, spec: SandboxSpec) -> LocalHandle:
        directory = tempfile.TemporaryDirectory(prefix="tf-local-sandbox-")
        self._dirs.append(directory)
        handle = LocalHandle(spec, Path(directory.name))
        self.specs.append(spec)
        self.handles.append(handle)
        return handle

    def cleanup(self) -> None:
        for directory in self._dirs:
            directory.cleanup()
