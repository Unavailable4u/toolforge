"""The registry: the only place that reads or writes tool state (`docs/guide/02-architecture.md`).

`Registry` ties together the database (`db`), the blob store (`store`), the lifecycle (`lifecycle`) and search (`search`) for one
data directory (`$TOOLFORGE_HOME`):

```
registry.sqlite3          versions, status, gate results, calls, approvals, search index
blobs/<sha256>.tar.gz     frozen packages, content addressed
```

The registry stores facts and enforces the lifecycle. It never runs tool code, never writes ledgers (`transition` returns the
event to append) and never decides who may call it: the MCP server must not expose its state-changing methods to an agent
(a test in P09 enforces that). Versions are immutable and rows are never deleted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from toolforge.errors import RegistryError
from toolforge.registry import lifecycle, search
from toolforge.registry.db import (
    GATE_STATUSES,
    STATS_MIN_CALLS,
    STATS_WINDOW,
    Database,
    Stats,
    VersionRecord,
    call_stats,
    record_call,
    row_to_version,
    utc_now,
)
from toolforge.registry.search import Hit
from toolforge.registry.store import BlobStore

if TYPE_CHECKING:
    from toolforge.config import Settings
    from toolforge.models.package import ToolPackage

DB_NAME = "registry.sqlite3"
BLOBS_DIR = "blobs"
MAX_GATE_DETAIL_BYTES = 64 * 1024
APPROVAL_STATUSES = ("pending", "approved", "denied")
DECISIONS = APPROVAL_STATUSES[1:]
_TEXT_CAP = 80


@dataclass(frozen=True)
class ToolSummary:
    name: str
    active_version: str | None
    created_at: str
    versions: int


@dataclass(frozen=True)
class GateResult:
    id: int
    version_id: int
    gate: str
    status: str
    detail: Any
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class Approval:
    id: int
    version_id: int
    requested: Any
    status: str
    decided_by: str | None
    decided_at: str | None
    created_at: str


def _plain(value: object, what: str, *, cap: int = _TEXT_CAP) -> str:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise RegistryError(f"{what} must be short plain text", code="invalid_value")
    return value.strip()[:cap]


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"the value is not plain JSON ({exc})", code="invalid_value") from None


class Registry:
    """Versions, their status, gate results, blobs, approvals, call statistics and search, in one data directory."""

    def __init__(self, home: str | Path, *, clock: Callable[[], str] | None = None) -> None:
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = Database(self.home / DB_NAME, clock=clock)
        self.blobs = BlobStore(self.home / BLOBS_DIR)

    @classmethod
    def from_settings(cls, settings: Settings) -> Registry:
        return cls(settings.home)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- versions ----------------------------------------------------------------------------------------------------------

    def add_version(self, package: ToolPackage, *, created_by: str) -> VersionRecord:
        """Record a new version in status `submitted`.

        The caller validates the package first (`ToolPackage.validate`); an invalid one raises `ValidationError` here. The
        manifest is stored as indexed metadata; the source is not stored at all, the blob is (see `attach_blob`).
        Raises `RegistryError` (`version_exists`) when this name and version are already registered: versions are immutable.
        """
        manifest = package.manifest.model_dump(mode="json")
        code_sha = package.code_sha
        creator = _plain(created_by, "created_by")
        now = self.db.now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tools (name, active_version, created_at) VALUES (?, NULL, ?)",
                (manifest["name"], now),
            )
            try:
                cursor = conn.execute(
                    "INSERT INTO versions (name, version, status, code_sha, blob_sha, manifest_json, created_by, "
                    "parent_version, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    (
                        manifest["name"],
                        manifest["version"],
                        lifecycle.SUBMITTED,
                        code_sha,
                        _json_text(manifest),
                        creator,
                        manifest.get("parent_version"),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise RegistryError(
                    f"{manifest['name']}@{manifest['version']} is already registered; versions are immutable",
                    code="version_exists",
                    hint="raise the version number and set parent_version to the version you are replacing",
                ) from None
            return self._require(cursor.lastrowid)

    def _require(self, version_id: int | None) -> VersionRecord:
        record = self.get_version(version_id) if version_id is not None else None
        if record is None:
            raise RegistryError(
                f"no version with id {version_id}",
                code="unknown_version",
                hint="list versions with `toolforge registry list`",
            )
        return record

    def get_version(self, version_id: int) -> VersionRecord | None:
        row = self.db.query_one("SELECT * FROM versions WHERE id = ?", (version_id,))
        return row_to_version(row) if row else None

    def find_version(self, name: str, version: str) -> VersionRecord | None:
        row = self.db.query_one("SELECT * FROM versions WHERE name = ? AND version = ?", (name, version))
        return row_to_version(row) if row else None

    def list_versions(self, name: str | None = None, status: str | None = None) -> list[VersionRecord]:
        """Versions ordered by name, then by when they were registered. Filter by tool name and/or status."""
        clauses, params = [], []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            row_to_version(r)
            for r in self.db.query(f"SELECT * FROM versions{where} ORDER BY name, id", params)
        ]

    def active_version(self, name: str) -> VersionRecord | None:
        row = self.db.query_one(
            "SELECT v.* FROM versions v JOIN tools t ON t.name = v.name AND t.active_version = v.version "
            "WHERE t.name = ? AND v.status = 'active'",
            (name,),
        )
        return row_to_version(row) if row else None

    def list_tools(self) -> list[ToolSummary]:
        rows = self.db.query(
            "SELECT t.name, t.active_version, t.created_at, COUNT(v.id) AS n FROM tools t "
            "LEFT JOIN versions v ON v.name = t.name GROUP BY t.name ORDER BY t.name"
        )
        return [ToolSummary(r["name"], r["active_version"], r["created_at"], r["n"]) for r in rows]

    def get_tool(self, name: str) -> ToolSummary | None:
        return next((tool for tool in self.list_tools() if tool.name == name), None)

    def attach_blob(self, version_id: int, blob_sha: str) -> None:
        """Record the frozen blob of a version. Allowed once, while the version is `submitted` or `verifying`.

        The blob must already be in the store (`registry.blobs.put`). Attaching the same blob again does nothing; a different
        one raises `RegistryError`, because a version's content never changes.
        """
        with self.db.transaction() as conn:
            record = self._require(version_id)
            if record.blob_sha is not None:
                if record.blob_sha == blob_sha:
                    return
                raise RegistryError(
                    f"{record.label} already has a frozen blob; a version's content never changes",
                    code="blob_already_set",
                    hint="submit the changed package as a new version",
                )
            if record.status not in (lifecycle.SUBMITTED, lifecycle.VERIFYING):
                raise RegistryError(
                    f"{record.label} is {record.status}; its blob can only be attached while it is submitted or verifying",
                    code="blob_not_attachable",
                )
            if not self.blobs.exists(blob_sha):
                raise RegistryError(
                    f"blob {str(blob_sha)[:12]} is not in the store",
                    code="blob_missing",
                    hint="store the bytes with registry.blobs.put first",
                )
            conn.execute(
                "UPDATE versions SET blob_sha = ?, updated_at = ? WHERE id = ?",
                (blob_sha, self.db.now(), version_id),
            )

    # -- lifecycle, search, statistics --------------------------------------------------------------------------------------

    def transition(
        self, version_id: int, to: str, *, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Move a version to a new status (see `lifecycle`). Returns the ledger event for the caller to append."""
        return lifecycle.transition(self.db, version_id, to, actor=actor, reason=reason)

    def search(self, query: str, limit: int = search.DEFAULT_LIMIT) -> list[Hit]:
        """Active tools that match `query`, best first."""
        return search.search(self.db, query, limit)

    def record_call(
        self,
        version_id: int,
        ok: bool,
        duration_ms: int,
        error_class: str | None = None,
        session_id: str | None = None,
    ) -> None:
        record_call(self.db, version_id, ok, duration_ms, error_class, session_id)

    def stats(self, target: int | str) -> Stats:
        """Statistics for a version id, for `name@version`, or for a tool name (its active version).

        `success_rate` is over the last 50 calls and is `None` ("not measured") until there are at least 5.
        """
        if isinstance(target, int) and not isinstance(target, bool):
            record = self.get_version(target)
        elif isinstance(target, str) and "@" in target:
            name, _, version = target.partition("@")
            record = self.find_version(name, version)
        elif isinstance(target, str):
            record = self.active_version(target)
        else:
            record = None
        if record is None:
            if isinstance(target, str) and "@" not in target and self.get_tool(target) is not None:
                return Stats(uses=0, recent_calls=0, success_rate=None)  # a known tool with no active version
            raise RegistryError(
                f"no such tool or version: {target!r}",
                code="unknown_version",
                hint="use a version id, name@version or a tool name",
            )
        return call_stats(self.db, record.id)

    # -- gate results ------------------------------------------------------------------------------------------------------

    def record_gate(
        self,
        version_id: int,
        gate: str,
        status: str,
        detail: Any = None,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> int:
        """Record the outcome of one gate run (`passed`, `failed` or `skipped`). Returns the new row id.

        `detail` must be plain JSON. A detail over 64 KB is replaced by a marker saying how large it was, so a verbose tool
        or sandbox cannot bloat the registry, while the outcome itself is still recorded.
        """
        if status not in GATE_STATUSES:
            raise RegistryError(
                f"gate status {status!r} is not one of {', '.join(GATE_STATUSES)}", code="invalid_value"
            )
        text = _json_text(None if detail is None else detail)
        if len(text) > MAX_GATE_DETAIL_BYTES:
            text = _json_text({"truncated": True, "size": len(text)})
        now = self.db.now()
        with self.db.transaction() as conn:
            self._require(version_id)
            cursor = conn.execute(
                "INSERT INTO gate_results (version_id, gate, status, detail_json, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    _plain(gate, "gate", cap=20),
                    status,
                    text,
                    started_at or now,
                    finished_at or now,
                ),
            )
            return int(cursor.lastrowid or 0)

    def gate_results(self, version_id: int) -> list[GateResult]:
        rows = self.db.query("SELECT * FROM gate_results WHERE version_id = ? ORDER BY id", (version_id,))
        return [
            GateResult(
                r["id"],
                r["version_id"],
                r["gate"],
                r["status"],
                json.loads(r["detail_json"]),
                r["started_at"],
                r["finished_at"],
            )
            for r in rows
        ]

    # -- approvals (storage only; the flow is patch P09) ---------------------------------------------------------------------

    def add_approval(self, version_id: int, requested: Mapping[str, Any]) -> Approval:
        """Open a pending approval request for a version. `requested` is plain JSON describing what is being asked."""
        with self.db.transaction() as conn:
            self._require(version_id)
            cursor = conn.execute(
                "INSERT INTO approvals (version_id, requested_json, status, created_at) VALUES (?, ?, 'pending', ?)",
                (version_id, _json_text(dict(requested)), self.db.now()),
            )
            return self._require_approval(int(cursor.lastrowid or 0))

    def _require_approval(self, approval_id: int) -> Approval:
        approval = self.get_approval(approval_id)
        if approval is None:
            raise RegistryError(f"no approval with id {approval_id}", code="unknown_approval")
        return approval

    @staticmethod
    def _approval(row: sqlite3.Row) -> Approval:
        return Approval(
            row["id"],
            row["version_id"],
            json.loads(row["requested_json"]),
            row["status"],
            row["decided_by"],
            row["decided_at"],
            row["created_at"],
        )

    def get_approval(self, approval_id: int) -> Approval | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return self._approval(row) if row else None

    def list_approvals(self, status: str | None = None) -> list[Approval]:
        if status is None:
            rows = self.db.query("SELECT * FROM approvals ORDER BY id")
        else:
            rows = self.db.query("SELECT * FROM approvals WHERE status = ? ORDER BY id", (status,))
        return [self._approval(r) for r in rows]

    def decide_approval(self, approval_id: int, status: str, *, decided_by: str) -> Approval:
        """Record a decision (`approved` or `denied`) on a pending request. A decided request cannot be changed."""
        if status not in DECISIONS:
            raise RegistryError(
                f"a decision is one of {', '.join(DECISIONS)}, not {status!r}", code="invalid_value"
            )
        who = _plain(decided_by, "decided_by")
        with self.db.transaction() as conn:
            current = self._require_approval(approval_id)
            if current.status != "pending":
                raise RegistryError(
                    f"approval {approval_id} was already {current.status}; a decision cannot be changed",
                    code="approval_decided",
                )
            conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                (status, who, self.db.now(), approval_id),
            )
            return self._require_approval(approval_id)


__all__ = [
    "APPROVAL_STATUSES",
    "STATS_MIN_CALLS",
    "STATS_WINDOW",
    "Approval",
    "GateResult",
    "Hit",
    "Registry",
    "Stats",
    "ToolSummary",
    "VersionRecord",
    "lifecycle",
    "search",
    "utc_now",
]
