"""The sandbox seam: everything that needs a sandbox talks to the two protocols and three dataclasses in this module.

Two implementations exist: `FakeRunner` (scripted, never runs code, for unit tests) and `MiniLockerRunner` (hardened Docker
containers). Both apply the same argument rules, which live here so the fake cannot be more lenient than the real thing.

Security properties: a handle is a context manager whose `__exit__` always calls `close`; `put` only writes under /workspace and
/tmp; `run` takes an argv list and never a shell string. Proven by `tests/test_fake_runner.py` (rules, context manager) and
`tests/docker/test_minilocker_runner.py` (the real containers).
"""

from __future__ import annotations

import copy
import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal, Protocol, Self

WORKSPACE = "/workspace"
WRITABLE_ROOTS = (WORKSPACE, "/tmp")
_BAD_HOST_CHARS = set(" /:*\\\t\r\n")


@dataclass(frozen=True)
class SandboxSpec:
    """What a caller asks for. Hardening (non-root, read-only root filesystem, no capabilities) is not optional and not here."""

    image: str
    network: Literal["none"] | tuple[str, ...]  # "none" or the hostnames reachable through the egress proxy
    workspace_mb: int = 64
    memory_mb: int = 256
    pids: int = 128
    env: Mapping[str, str] = field(default_factory=dict)
    decoys: bool = False  # plant MiniLocker decoy credentials (Watch)
    ledger_path: str | None = None
    label: str = ""  # free text for ledger events: "verify:csv_summary@1.0.0:G3"

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or not self.image.strip():
            raise ValueError("SandboxSpec.image must be a non-empty image name")
        if self.network != "none":
            hosts = self.network
            if isinstance(hosts, str) or not isinstance(hosts, Iterable):
                raise ValueError('SandboxSpec.network must be "none" or a tuple of host names')
            cleaned = tuple(dict.fromkeys(str(h).strip().lower() for h in hosts))  # de-duplicated, order kept
            if not cleaned or any(not h or _BAD_HOST_CHARS & set(h) for h in cleaned):
                raise ValueError(
                    "SandboxSpec.network hosts must be plain host names (no scheme, port, path or wildcard); "
                    'use "none" for no network'
                )
            object.__setattr__(self, "network", cleaned)
        for name in ("workspace_mb", "memory_mb", "pids"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"SandboxSpec.{name} must be a positive integer")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in self.env.items()):
            raise ValueError("SandboxSpec.env must map strings to strings")
        object.__setattr__(self, "env", dict(self.env))  # a private copy: the caller may keep mutating theirs


@dataclass
class RunOutput:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool


@dataclass
class AuditSummary:
    """What the sandbox measured about itself, taken just before it was destroyed.

    An empty `hardening` or `measured` means "not measured", never "nothing happened": check `measured["incomplete"]` for the
    parts the audit could not do.
    """

    hardening: dict
    measured: dict
    decoy_events: list[dict]
    egress_attempts: list[dict]
    ledger_path: str | None
    destroyed: bool


class SandboxHandle(Protocol):
    """One sandbox. Used by one thread at a time; always closed (use it as a context manager)."""

    def put(self, path: str, data: bytes) -> None: ...
    def run(self, argv: list[str], timeout_s: int, max_out: int) -> RunOutput: ...
    def freeze(self) -> bytes: ...
    def thaw(self, blob: bytes) -> None: ...
    def events(self) -> list[dict]: ...
    def close(self) -> AuditSummary: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...


class SandboxRunner(Protocol):
    """Opens sandboxes. Safe to use from several threads. Raises `SandboxUnavailable` when it cannot provide one."""

    def open(self, spec: SandboxSpec) -> SandboxHandle: ...


class ContextHandle:
    """`__enter__`/`__exit__` for handle classes: leaving the block always closes the sandbox, also after an exception."""

    def close(self) -> AuditSummary:  # pragma: no cover - overridden
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


def normalize_path(path: str) -> str:
    """The absolute POSIX path a `put` will write, or ValueError. Relative paths resolve under /workspace (the working directory)."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("path must be a non-empty string without NUL")
    joined = path if path.startswith("/") else posixpath.join(WORKSPACE, path)
    normal = posixpath.normpath(joined)
    if normal in WRITABLE_ROOTS:
        raise ValueError(f"path names the directory {normal}; give a file path")
    if not any(normal.startswith(root + "/") for root in WRITABLE_ROOTS):
        raise ValueError(
            f"path {path!r} is outside {' and '.join(WRITABLE_ROOTS)}, the only places a sandbox may be written"
        )
    return normal


def check_run_args(argv: Any, timeout_s: Any, max_out: Any) -> list[str]:
    """Validate `run` arguments the same way for every implementation; returns argv as a list."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings (it is run without a shell)")
    if any("\x00" in a for a in argv):
        raise ValueError("argv must not contain NUL")
    for name, value in (("timeout_s", timeout_s), ("max_out", max_out)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    return list(argv)


def split_events(events: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """(decoy events, egress events) from a ledger event list: MiniLocker's native `canary.*` and `egress.*` names.

    The decoy events include `canary.seeded`, which only says the decoys were planted. Most callers want `decoy_violations`.
    """
    decoys = [copy.deepcopy(e) for e in events if str(e.get("type", "")).startswith("canary.")]
    egress = [copy.deepcopy(e) for e in events if str(e.get("type", "")).startswith("egress.")]
    return decoys, egress


# What MiniLocker's `Watch` logs when something touched a planted decoy (trust/canary.py says what each means):
#   read    a decoy file's access time moved          exposed  a decoy value was in what a command printed
#   tripped a decoy value was used (for example as a host name)   copied  a decoy value was found in a file left behind
# `canary.seeded` (planted) and `canary.unavailable` (could not plant) are not violations. This is the one definition: gate G6 and the
# runtime tripwire both read it, so what fails verification is exactly what quarantines a tool that is already active.
DECOY_VIOLATION_TYPES = ("canary.read", "canary.exposed", "canary.tripped", "canary.copied")
DECOY_PLANTED_TYPE = "canary.seeded"


def decoy_violations(events: Iterable[dict]) -> list[dict]:
    """The events that say a decoy was touched, in order. Empty means none was seen, not that decoys were planted."""
    return [copy.deepcopy(e) for e in events if e.get("type") in DECOY_VIOLATION_TYPES]


def decoys_planted(events: Iterable[dict]) -> int:
    """How many decoys the sandbox says it planted (0 when no `canary.seeded` event exists: then nothing was watched)."""
    for event in events:
        if event.get("type") == DECOY_PLANTED_TYPE:
            items = (event.get("payload") or {}).get("items")
            return len(items) if isinstance(items, list) else 0
    return 0


# `docker diff` lists the directory that a tmpfs is mounted on (Docker creates or touches it in the container's own layer when it
# mounts). A real run showed `/workspace` as the one change of a tool that wrote nothing. A tool cannot add, remove or change a mount
# point itself: the root file system is read-only and what it writes lands inside the tmpfs, which `docker diff` does not list. So an
# entry whose path is exactly a mount point, of any kind, is the sandbox's own doing and is not counted. Every other path counts.
MOUNT_POINTS = (WORKSPACE, "/tmp")


def real_rootfs_changes(count: object, listed: object, mounts: object = ()) -> tuple[int, list[dict], int]:
    """`(changes, shown, ignored)` for a sandbox audit: container file changes without the sandbox's own mount points.

    `count` is MiniLocker's `rootfs_changes`, `listed` its `rootfs_changed` entries (`{"path", "kind"}`, only the first few) and
    `mounts` the tmpfs paths it reported. Only an entry whose path is exactly a mount point is ignored, whatever its kind; every other
    path still counts. Anything that is not a number or a list is read as empty, so a
    garbled audit can lower nothing it did not name: `changes` is the reported count minus the entries that were ignored.
    """
    raw = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
    entries = [e for e in listed if isinstance(e, dict)] if isinstance(listed, list) else []
    points = set(MOUNT_POINTS)
    if isinstance(mounts, (list, tuple, set)):
        points |= {m for m in mounts if isinstance(m, str)}
    shown = [e for e in entries if e.get("path") not in points]
    ignored = len(entries) - len(shown)
    return max(raw - ignored, 0), shown, ignored
