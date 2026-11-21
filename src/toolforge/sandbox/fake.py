"""A scripted `SandboxRunner` for unit tests. It never executes anything.

Tests register expectations, either a function `(argv, put_files) -> RunOutput` or a list of canned `RunOutput`s (consumed one per
`run`, per handle). With no expectation a `run` fails loudly instead of inventing a result. The runner records every
`open(spec)` so a test can assert the network rule, limits and decoy flag that production code asked for.

Honesty rules (docs/guide/patches/P04-sandbox-seam.md): no code is run, so this module imports nothing that could run one
(`tests/test_fake_runner.py` checks the imports); a fake never measured anything, so its `hardening` and `measured` are empty
unless the test supplies them; it writes no ledger file, whatever `ledger_path` says.
"""

from __future__ import annotations

import copy
import gzip
import io
import posixpath
import tarfile
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from toolforge.errors import SandboxError, SandboxUnavailable
from toolforge.sandbox.base import (
    WORKSPACE,
    AuditSummary,
    ContextHandle,
    RunOutput,
    SandboxSpec,
    check_run_args,
    normalize_path,
    split_events,
)

Responder = Callable[[list[str], Mapping[str, bytes]], RunOutput]
Script = Responder | Sequence[RunOutput]

STDERR_CAP = 65536  # MiniLocker's exec_capped keeps at most this much stderr


@dataclass(frozen=True)
class RunCall:
    argv: list[str]
    timeout_s: int
    max_out: int


class FakeHandle(ContextHandle):
    def __init__(
        self,
        spec: SandboxSpec,
        script: Script | None,
        hardening: dict | None = None,
        measured: dict | None = None,
        events: Sequence[tuple[str, dict]] | None = None,
    ) -> None:
        self.spec = spec
        self.files: dict[str, bytes] = {}  # everything `put` or `thaw` wrote, by absolute path
        self.calls: list[RunCall] = []
        self.closed = False
        self.dead = False  # a scripted timeout kills the sandbox, as it does a real container
        self._responder: Responder | None = None
        self._canned: deque[RunOutput] | None = None
        if callable(script):
            self._responder = script
        elif script is not None:
            self._canned = deque(script)
        self._hardening = dict(hardening or {})
        self._measured = dict(measured or {})
        self._events: list[dict] = []
        self._summary: AuditSummary | None = None
        for type, payload in (
            events or ()
        ):  # what a test says MiniLocker logged, present in every handle from the start
            self.add_event(type, payload)

    def _check_usable(self) -> None:
        if self.closed:
            raise SandboxError("the sandbox is closed", hint="open a new one")
        if self.dead:
            raise SandboxError("the sandbox was killed by a timeout", hint="open a new one")

    def put(self, path: str, data: bytes) -> None:
        self._check_usable()
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("data must be bytes")
        self.files[normalize_path(path)] = bytes(data)

    def run(self, argv: list[str], timeout_s: int, max_out: int) -> RunOutput:
        self._check_usable()
        args = check_run_args(argv, timeout_s, max_out)
        self.calls.append(RunCall(args, timeout_s, max_out))
        if self._responder is not None:
            result = self._responder(list(args), dict(self.files))
        elif self._canned:
            result = self._canned.popleft()
        else:
            raise AssertionError(
                f"FakeRunner has no expectation for run({args!r}): register one with FakeRunner(script=...) or .expect(...)"
            )
        if not isinstance(result, RunOutput):
            raise AssertionError(
                f"a FakeRunner expectation must return RunOutput, not {type(result).__name__}"
            )
        stdout, truncated = result.stdout, result.truncated or len(result.stdout) > max_out
        if len(stdout) > max_out:
            stdout = stdout[:max_out]  # the real runner caps stdout the same way
        if result.timed_out:
            self.dead = True
        return RunOutput(result.exit_code, stdout, result.stderr[:STDERR_CAP], result.timed_out, truncated)

    def freeze(self) -> bytes:
        """A gzipped tar of /workspace in the order of the paths, with fixed times and owners, so equal content gives equal bytes."""
        self._check_usable()
        buffer = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for path in sorted(self.files):
                    if not path.startswith(WORKSPACE + "/"):
                        continue  # /tmp is not part of a workspace snapshot
                    data = self.files[path]
                    info = tarfile.TarInfo("./" + path[len(WORKSPACE) + 1 :])
                    info.size, info.mtime, info.mode = len(data), 0, 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def thaw(self, blob: bytes) -> None:
        """Restore what `freeze` made. Anything that is not a plain, in-workspace regular file is an error."""
        self._check_usable()
        if not isinstance(blob, (bytes, bytearray)) or not blob:
            raise SandboxError("thaw needs the bytes that freeze returned")
        restored: dict[str, bytes] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(bytes(blob)), mode="r:gz") as tar:
                for member in tar.getmembers():
                    name = posixpath.normpath(member.name)
                    if member.isdir():
                        continue
                    if not member.isreg() or name.startswith(("/", "..")):
                        raise SandboxError(
                            f"unsupported or unsafe entry in the snapshot: {member.name[:80]!r}"
                        )
                    extracted = tar.extractfile(member)
                    restored[posixpath.join(WORKSPACE, name)] = extracted.read() if extracted else b""
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise SandboxError(f"could not restore the snapshot: {type(exc).__name__}") from exc
        self.files.update(restored)

    def add_event(self, type: str, payload: dict | None = None, actor: str = "fake") -> None:
        """Let a test simulate what MiniLocker would have logged, for example a `canary.read` or an `egress.blocked`."""
        self._events.append(
            {"id": len(self._events), "actor": actor, "type": type, "payload": copy.deepcopy(payload or {})}
        )

    def events(self) -> list[dict]:
        return copy.deepcopy(self._events)

    def close(self) -> AuditSummary:
        if self._summary is None:
            decoys, egress = split_events(self._events)
            self._summary = AuditSummary(
                hardening=dict(self._hardening),
                measured=dict(self._measured),
                decoy_events=decoys,
                egress_attempts=egress,
                ledger_path=self.spec.ledger_path,
                destroyed=True,
            )
            self.closed = True
        return self._summary


class FakeRunner:
    """Records every `open(spec)` and hands out scripted handles. Safe to use from several threads."""

    def __init__(
        self,
        script: Script | None = None,
        *,
        available: bool = True,
        hardening: dict | None = None,
        measured: dict | None = None,
        events: Sequence[tuple[str, dict]] | None = None,
    ) -> None:
        self.opened: list[SandboxSpec] = []  # every spec ever passed to `open`, including refused ones
        self.handles: list[FakeHandle] = []
        self.available = available
        self._script = script
        self._hardening = hardening
        self._measured = measured
        self._events = events
        self._lock = threading.Lock()

    def expect(self, script: Script) -> None:
        """Replace the expectation. It applies to handles opened after this call."""
        with self._lock:
            self._script = script

    def open(self, spec: SandboxSpec) -> FakeHandle:
        if not isinstance(spec, SandboxSpec):
            raise TypeError("open() takes a SandboxSpec")
        with self._lock:
            self.opened.append(spec)
            if not self.available:
                raise SandboxUnavailable(
                    "the FakeRunner was told to be unavailable",
                    hint="this simulates a machine without Docker",
                )
            handle = FakeHandle(spec, self._script, self._hardening, self._measured, self._events)
            self.handles.append(handle)
            return handle
