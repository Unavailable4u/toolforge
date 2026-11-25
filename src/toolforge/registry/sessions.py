"""What the registry remembers about sessions (P13): one row per session, plain storage.

A session's sandbox lives in the process that opened it, so this table is not the session. It is what *other* processes can see:
`toolforge sessions list`, `toolforge doctor` and a second server on the same data directory. Nothing here decides anything; the rules
(time limits, how many may be open, who may call) are `services/sessions.py`, and the lifecycle of the tool version is `lifecycle`.

* A session id has two parts, `<public>.<secret>`. The public part names the session in lists and in the operator's tools. The secret
  part is what lets a caller use it, and only its SHA-256 is stored. MCP carries no caller identity, so holding the whole id is the
  only proof that a session is yours; the caller who opened it is the only one who ever saw the secret.
* `owner` is a random token of the process that holds the sandbox, and `heartbeat_at` is refreshed by that process every few seconds. An
  `open` row whose heartbeat is old belongs to a process that died; its container is a leftover, and `orphaned` says so.
* Rows are never deleted. A closed session keeps its row (state and reason) so the history shows what happened.

Proven by `tests/test_registry_sessions.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from toolforge.errors import RegistryError
from toolforge.registry.db import Database

STATES = ("open", "closed", "expired", "quarantined", "lost", "operator")
CLOSED_STATES = STATES[1:]
ORPHAN_AFTER_S = 45.0  # an open session whose owner has not reported for this long is orphaned (the reaper reports every 5 s)
_PUBLIC_CHARS = 8
_SECRET_CHARS = 32
_REASON_CAP = 200


def new_session_id() -> tuple[str, str, str]:
    """`(session_id, public_id, secret_sha256)`. The full id is returned once, to the caller who opened the session."""
    public = "s-" + secrets.token_hex(_PUBLIC_CHARS // 2)
    secret = secrets.token_hex(_SECRET_CHARS // 2)
    return f"{public}.{secret}", public, hashlib.sha256(secret.encode("ascii")).hexdigest()


def split_session_id(value: object) -> tuple[str, str] | None:
    """`(public, secret)` when `value` has the shape of a session id, else None. Says nothing about whether it exists."""
    if not isinstance(value, str) or len(value) != 2 + _PUBLIC_CHARS + 1 + _SECRET_CHARS:
        return None
    public, dot, secret = value.partition(".")
    if dot != "." or not public.startswith("s-"):
        return None
    hexdigits = set("0123456789abcdef")
    if not (set(public[2:]) <= hexdigits and set(secret) <= hexdigits) or len(public) != 2 + _PUBLIC_CHARS:
        return None
    return public, secret


def secret_matches(stored_sha256: str, secret: str) -> bool:
    return hmac.compare_digest(stored_sha256, hashlib.sha256(secret.encode("ascii", "replace")).hexdigest())


@dataclass(frozen=True)
class SessionRow:
    public_id: str
    secret_sha256: str
    version_id: int
    label: str
    state: str
    reason: str | None
    owner: str
    box: str | None
    steps: int
    rewinds: int
    close_requested: bool
    opened_at: float
    last_used_at: float
    expires_at: float
    hard_expires_at: float
    heartbeat_at: float
    closed_at: float | None

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def orphaned(self, now: float) -> bool:
        """Open, and its owner has been silent for too long to still be running."""
        return self.is_open and now - self.heartbeat_at > ORPHAN_AFTER_S

    def due(self, now: float) -> str | None:
        """Why this open session should be closed by now (`idle` or `max_lifetime`), else None."""
        if not self.is_open:
            return None
        if now >= self.hard_expires_at:
            return "max_lifetime"
        if now >= self.expires_at:
            return "idle"
        return None


def _row(row: object) -> SessionRow:
    r = row  # a sqlite3.Row
    return SessionRow(
        public_id=r["public_id"],  # type: ignore[index]
        secret_sha256=r["secret_sha256"],  # type: ignore[index]
        version_id=int(r["version_id"]),  # type: ignore[index]
        label=r["label"],  # type: ignore[index]
        state=r["state"],  # type: ignore[index]
        reason=r["reason"],  # type: ignore[index]
        owner=r["owner"],  # type: ignore[index]
        box=r["box"],  # type: ignore[index]
        steps=int(r["steps"]),  # type: ignore[index]
        rewinds=int(r["rewinds"]),  # type: ignore[index]
        close_requested=bool(r["close_requested"]),  # type: ignore[index]
        opened_at=float(r["opened_at"]),  # type: ignore[index]
        last_used_at=float(r["last_used_at"]),  # type: ignore[index]
        expires_at=float(r["expires_at"]),  # type: ignore[index]
        hard_expires_at=float(r["hard_expires_at"]),  # type: ignore[index]
        heartbeat_at=float(r["heartbeat_at"]),  # type: ignore[index]
        closed_at=None if r["closed_at"] is None else float(r["closed_at"]),  # type: ignore[index]
    )


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return "".join(c if c.isprintable() else "?" for c in text)[:_REASON_CAP]


def open_session(
    db: Database,
    *,
    public_id: str,
    secret_sha256: str,
    version_id: int,
    label: str,
    owner: str,
    box: str | None,
    now: float,
    ttl_s: int,
    max_s: int,
) -> SessionRow:
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM versions WHERE id = ?", (version_id,)).fetchone() is None:
            raise RegistryError(f"no version with id {version_id}", code="unknown_version")
        conn.execute(
            "INSERT INTO sessions (public_id, secret_sha256, version_id, label, state, owner, box, opened_at, last_used_at, "
            "expires_at, hard_expires_at, heartbeat_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
            (
                public_id,
                secret_sha256,
                version_id,
                label,
                owner,
                box,
                now,
                now,
                min(now + ttl_s, now + max_s),
                now + max_s,
                now,
            ),
        )
    row = get(db, public_id)
    assert row is not None
    return row


def get(db: Database, public_id: str) -> SessionRow | None:
    found = db.query_one("SELECT * FROM sessions WHERE public_id = ?", (public_id,))
    return _row(found) if found is not None else None


def list_sessions(db: Database, *, state: str | None = None, limit: int = 100) -> list[SessionRow]:
    if state is not None and state not in STATES:
        raise RegistryError(f"unknown session state {state!r}", code="invalid_state")
    sql = (
        "SELECT * FROM sessions" + (" WHERE state = ?" if state else "") + " ORDER BY opened_at DESC LIMIT ?"
    )
    params = (state, limit) if state else (limit,)
    return [_row(r) for r in db.query(sql, params)]


def count_open(db: Database, *, owner: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM sessions WHERE state = 'open'" + (" AND owner = ?" if owner else "")
    found = db.query_one(sql, (owner,) if owner else ())
    return int(found["n"]) if found else 0


def touch(
    db: Database, public_id: str, *, now: float, ttl_s: int, box: str | None = None, steps: int | None = None
) -> None:
    """A call finished: the idle clock restarts (never past the hard limit), and the step count and box name are updated."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET last_used_at = ?, expires_at = MIN(? , hard_expires_at), "
            "box = COALESCE(?, box), steps = COALESCE(?, steps) WHERE public_id = ? AND state = 'open'",
            (now, now + ttl_s, box, steps, public_id),
        )


def note_rewind(db: Database, public_id: str, *, box: str | None, steps: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET rewinds = rewinds + 1, box = ?, steps = ? WHERE public_id = ? AND state = 'open'",
            (box, steps, public_id),
        )


def heartbeat(db: Database, owner: str, *, now: float) -> int:
    """The owner process says it is alive. Returns how many open sessions it holds."""
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE sessions SET heartbeat_at = ? WHERE owner = ? AND state = 'open'", (now, owner)
        )
        return int(cursor.rowcount)


def request_close(db: Database, public_id: str) -> bool:
    """Ask the owning process to close the session (the operator's `sessions close`). True when an open session was marked."""
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE sessions SET close_requested = 1 WHERE public_id = ? AND state = 'open'", (public_id,)
        )
        return int(cursor.rowcount) > 0


def close(db: Database, public_id: str, state: str, reason: str | None, *, now: float) -> bool:
    """Mark a session closed. True when this call did it; False when it was not open (a second close is not an error)."""
    if state not in CLOSED_STATES:
        raise RegistryError(f"{state!r} is not a closed session state", code="invalid_state")
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE sessions SET state = ?, reason = ?, closed_at = ?, box = NULL WHERE public_id = ? AND state = 'open'",
            (state, _clip(reason), now, public_id),
        )
        return int(cursor.rowcount) > 0


def open_for_version(db: Database, version_id: int) -> list[SessionRow]:
    return [
        _row(r)
        for r in db.query("SELECT * FROM sessions WHERE version_id = ? AND state = 'open'", (version_id,))
    ]


__all__ = [
    "CLOSED_STATES",
    "ORPHAN_AFTER_S",
    "STATES",
    "SessionRow",
    "close",
    "count_open",
    "get",
    "heartbeat",
    "list_sessions",
    "new_session_id",
    "note_rewind",
    "open_for_version",
    "open_session",
    "request_close",
    "secret_matches",
    "split_session_id",
    "touch",
]
