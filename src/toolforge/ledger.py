"""The ledger: a hash-chained, append-only event file, written through MiniLocker's `Ledger` so its report builder can read it.

MiniLocker's `Ledger.append` writes the line but neither creates the directory nor forces it to disk. This wrapper does both: it
creates parent directories on first use and fsyncs after every append, so an event that `append` returned is on disk even if the
process dies a moment later. Events carry ids, hashes, counts and labels, never secret values (docs/guide/01-conventions.md).

Proven by `tests/test_ledger.py` (directory creation, fsync per append, chain verification, tamper detection, resuming a file).
Needs MiniLocker; without the submodule construction raises `SandboxUnavailable` (see `sandbox/_ml_path.py`).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from toolforge.errors import LedgerError
from toolforge.sandbox import _ml_path


class ToolforgeLedger:
    """`ToolforgeLedger(path, task_id)`; `path=None` keeps the events in memory only (nothing is written)."""

    def __init__(self, path: str | os.PathLike[str] | None, task_id: str) -> None:
        chain = _ml_path.load("minilocker.ledger.chain")
        self.path = os.fspath(path) if path is not None else None
        self.task_id = task_id
        self._lock = threading.Lock()
        if self.path is not None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._ledger: Any = chain.Ledger(
                task_id, self.path
            )  # resumes the chain when the file already exists
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            raise LedgerError(
                f"the ledger file {self.path} is not valid: {str(exc)[:120]}",
                hint="do not edit ledger files; move the damaged one aside and start a new run",
            ) from exc

    def append(self, actor: str, type: str, payload: dict | None = None) -> dict:
        """Add one event and make it durable before returning it."""
        with self._lock:
            event = self._ledger.append(actor, type, payload)
            if self.path is not None:
                with open(
                    self.path, "ab"
                ) as handle:  # appends nothing; flush and fsync make the line durable
                    handle.flush()
                    os.fsync(handle.fileno())
            return event

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return list(self._ledger.events)

    def verify(self) -> tuple[bool, str]:
        """(ok, message): the chain is intact. MiniLocker's `verify_events`, run on what this ledger holds."""
        with self._lock:
            return self._ledger.verify()
