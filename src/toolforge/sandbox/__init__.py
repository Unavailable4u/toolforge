"""The sandbox seam (docs/guide/03-minilocker-integration.md). Importing this package never imports MiniLocker or Docker.

`MiniLockerRunner` and `EgressPool` are loaded on first use: `toolforge.ledger` and `toolforge.textsafety` import
`sandbox._ml_path`, and the runner imports both of them, so importing the runner here eagerly would be circular.
"""

from typing import Any

from toolforge.sandbox.base import (
    AuditSummary,
    RunOutput,
    SandboxHandle,
    SandboxRunner,
    SandboxSpec,
)
from toolforge.sandbox.fake import FakeRunner

__all__ = [
    "AuditSummary",
    "EgressPool",
    "FakeRunner",
    "MiniLockerRunner",
    "RunOutput",
    "SandboxHandle",
    "SandboxRunner",
    "SandboxSpec",
]

_LAZY = {"EgressPool", "MiniLockerRunner"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from toolforge.sandbox import minilocker_runner

        return getattr(minilocker_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
