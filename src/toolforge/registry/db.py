"""The registry database: connections, migrations, transactions, and the call statistics.

SQLite in WAL mode with a busy timeout, standard library only. Two rules keep it correct:

* Every write goes through `Database.transaction()`. It takes one process-wide lock (shared by every `Database` that opens
  the same file), then `BEGIN IMMEDIATE`, so a read-then-write inside it cannot interleave with another writer, in this process
  or in another one (the dashboard is a separate process). Nested use becomes a savepoint, so an inner failure undoes only
  the inner work. Proved by `tests/test_registry_db.py` (rollback, nesting, threads).
* Reads outside a transaction use one read-only connection per thread and never wait for a writer. Reads inside a transaction
  use the writer's connection, so code sees its own uncommitted changes.

Nothing is ever deleted: statuses and rows are the history. All timestamps are UTC ISO strings ending in `Z`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from toolforge.errors import RegistryError

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000
STATS_WINDOW = 50  # the success rate is computed over this many most recent calls
STATS_MIN_CALLS = 5  # with fewer calls than this the rate is "not measured" (None), never 0
GATE_STATUSES = ("passed", "failed", "skipped")


def utc_now() -> str:
    """The current time as a UTC ISO string with milliseconds, for example `2026-11-19T07:44:12.123Z`."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --- Migrations ----------------------------------------------------------------------------------------------------------


class Migration(NamedTuple):
    version: int
    name: str
    steps: tuple[str | Callable[[sqlite3.Connection], None], ...]  # SQL statements or functions, run in order


# `tool_search` is an FTS5 table whose rowid is `versions.id`; only `active` versions have a row. The tokenizer splits on
# underscores, so `word_count` is found by "word" and "count", and stems, so "counting" finds "count".
_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS tool_search USING fts5("
    "name, description, when_to_use, examples_text, tokenize = 'porter unicode61')"
)


def _create_search_index(conn: sqlite3.Connection) -> None:
    """Create the FTS5 table, or note that this SQLite build has no FTS5 (search then falls back to Python)."""
    try:
        conn.execute(_FTS_DDL)
    except sqlite3.OperationalError as exc:
        log.warning(
            "SQLite has no FTS5 (%s); registry search will use the slower token-overlap fallback", exc
        )


_INITIAL_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE tools (
        name TEXT PRIMARY KEY,
        active_version TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE versions (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL REFERENCES tools(name),
        version TEXT NOT NULL,
        status TEXT NOT NULL,
        code_sha TEXT NOT NULL,
        blob_sha TEXT,
        manifest_json TEXT NOT NULL,
        created_by TEXT NOT NULL,
        parent_version TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (name, version)
    )""",
    "CREATE INDEX versions_by_status ON versions(status)",
    """CREATE TABLE gate_results (
        id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL REFERENCES versions(id),
        gate TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'skipped')),
        detail_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL
    )""",
    "CREATE INDEX gate_results_by_version ON gate_results(version_id)",
    """CREATE TABLE calls (
        id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL REFERENCES versions(id),
        ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
        duration_ms INTEGER NOT NULL,
        error_class TEXT,
        session_id TEXT,
        ts TEXT NOT NULL
    )""",
    "CREATE INDEX calls_by_version ON calls(version_id, id)",
    """CREATE TABLE approvals (
        id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL REFERENCES versions(id),
        requested_json TEXT NOT NULL,
        status TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX approvals_by_status ON approvals(status)",
)

# P12. `regression_cases` holds one row per captured failure of a call: the error class and a shape fingerprint (types and sizes, never
# values) always, and the raw arguments (`input_json`) only when the operator turned `store_failed_inputs` on and the input was small and
# free of secret-shaped text. A row with an input is a regression case that later versions of the tool are run against. `problem_reports`
# are what agents say about a version; they are only ever shown to the operator and never change a status. `repairs` is one row per repair
# attempt, which is what the per-hour limit and the "flagged for the operator" rule count.
_P12_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE regression_cases (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        version_id INTEGER NOT NULL REFERENCES versions(id),
        error_class TEXT NOT NULL,
        shape_json TEXT NOT NULL,
        input_json TEXT,
        input_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX regression_cases_by_tool ON regression_cases(name, id)",
    """CREATE TABLE problem_reports (
        id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL REFERENCES versions(id),
        reporter TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX problem_reports_by_version ON problem_reports(version_id, id)",
    """CREATE TABLE repairs (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        base_version TEXT NOT NULL,
        outcome TEXT NOT NULL,
        child_version TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT
    )""",
    "CREATE INDEX repairs_by_tool ON repairs(name, id)",
)

# P13. One row per session. The row is what other processes see (`toolforge sessions list`, `toolforge doctor`): the live sandbox itself
# belongs to the process that opened it. `public_id` is the part of a session id that may be shown; the caller's secret part is stored only
# as a hash (`secret_sha256`). Times are seconds since the epoch. `heartbeat_at` is refreshed by the owning process's reaper, so a session
# that is `open` with an old heartbeat belongs to a process that died. `box` is the container name of the sandbox that is open now.
_P13_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE sessions (
        public_id TEXT PRIMARY KEY,
        secret_sha256 TEXT NOT NULL,
        version_id INTEGER NOT NULL REFERENCES versions(id),
        label TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('open', 'closed', 'expired', 'quarantined', 'lost', 'operator')),
        reason TEXT,
        owner TEXT NOT NULL,
        box TEXT,
        steps INTEGER NOT NULL DEFAULT 0,
        rewinds INTEGER NOT NULL DEFAULT 0,
        close_requested INTEGER NOT NULL DEFAULT 0,
        opened_at REAL NOT NULL,
        last_used_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        hard_expires_at REAL NOT NULL,
        heartbeat_at REAL NOT NULL,
        closed_at REAL
    )""",
    "CREATE INDEX sessions_by_state ON sessions(state, opened_at)",
    "CREATE INDEX sessions_by_version ON sessions(version_id, state)",
)

# Append only. A released migration is never edited: later patches add a new entry.
MIGRATIONS: list[Migration] = [
    Migration(1, "initial schema", _INITIAL_SCHEMA),
    Migration(2, "search index", (_create_search_index,)),
    Migration(3, "regression cases, problem reports and repairs", _P12_SCHEMA),
    Migration(4, "session metadata", _P13_SCHEMA),
]


# --- Records -------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionRecord:
    """One row of `versions`. The tool source is never here: the blob is the source of truth, this is indexed metadata."""

    id: int
    name: str
    version: str
    status: str
    code_sha: str
    blob_sha: str | None
    manifest: dict[str, Any]
    created_by: str
    parent_version: str | None
    created_at: str
    updated_at: str

    @property
    def permissions(self) -> dict[str, Any]:
        value = self.manifest.get("permissions")
        return value if isinstance(value, dict) else {}

    @property
    def asks_network_or_secrets(self) -> bool:
        """True when the manifest declares any network host or secret: such a version needs operator approval."""
        return bool(self.permissions.get("network") or self.permissions.get("secrets"))

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


def row_to_version(row: sqlite3.Row) -> VersionRecord:
    return VersionRecord(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        status=row["status"],
        code_sha=row["code_sha"],
        blob_sha=row["blob_sha"],
        manifest=json.loads(row["manifest_json"]),
        created_by=row["created_by"],
        parent_version=row["parent_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@dataclass(frozen=True)
class Stats:
    """Call statistics for one version. `success_rate` is None (not measured) until there are enough recent calls."""

    uses: int  # every call ever recorded
    recent_calls: int  # calls inside the window (at most STATS_WINDOW)
    success_rate: (
        float | None
    )  # fraction of ok calls among the recent ones; None when too few, never a fake 0
    recent_ok: int = (
        0  # how many of the recent calls were ok: what a smoothed rate (search ranking, P12) is made from
    )


# --- The database --------------------------------------------------------------------------------------------------------

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    """One lock per database file for the whole process, however many `Database` objects open it."""
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class Database:
    """A migrated SQLite file with serialised writes. Create one per data directory and share it between threads."""

    def __init__(self, path: str | Path, *, clock: Callable[[], str] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or utc_now
        self._lock = _lock_for(self.path)
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_guard = threading.Lock()
        self._owner: int | None = None  # thread id that holds the open write transaction
        self._depth = 0
        self._closed = False
        self._writer = self._connect(read_only=False)
        try:
            self.migrate()
        except BaseException:
            self.close()  # do not leak the connection of a database that failed to open
            raise
        # Whether search can use FTS5. Tests flip this to exercise the fallback.
        self.fts_available: bool = self._table_exists("tool_search")

    # -- connections -------------------------------------------------------------------------------------------------------

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _check_open(self) -> None:
        if self._closed:
            raise RegistryError("the registry is closed", code="registry_closed")

    def _reader(self) -> sqlite3.Connection:
        self._check_open()
        if self._owner == threading.get_ident():
            return self._writer  # inside this thread's own transaction: see its uncommitted changes
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect(read_only=True)
            self._local.conn = conn
            with self._readers_guard:
                self._readers.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with self._readers_guard:
                readers, self._readers = self._readers, []
            for conn in readers:
                conn.close()
            self._writer.close()

    # -- reading and writing -----------------------------------------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._reader().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._reader().execute(sql, params).fetchone()

    @property
    def in_transaction(self) -> bool:
        return self._owner == threading.get_ident()

    def now(self) -> str:
        return self._clock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block of writes atomically. Raises inside the block roll everything back; nested blocks are savepoints."""
        self._check_open()
        me = threading.get_ident()
        with self._lock:
            conn = self._writer
            if self._owner == me:
                name = f"nested_{self._depth}"
                self._depth += 1
                conn.execute(f"SAVEPOINT {name}")
                try:
                    yield conn
                except BaseException:
                    conn.execute(f"ROLLBACK TO {name}")
                    conn.execute(f"RELEASE {name}")
                    raise
                else:
                    conn.execute(f"RELEASE {name}")
                finally:
                    self._depth -= 1
                return
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise RegistryError(
                    f"the registry database is busy or unavailable ({exc})",
                    code="registry_busy",
                    hint="another ToolForge process may be holding a long write; retry in a moment",
                ) from exc
            self._owner, self._depth = me, 1
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                self._owner, self._depth = None, 0

    # -- migrations --------------------------------------------------------------------------------------------------------

    def _table_exists(self, name: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')", (name,)
        )
        return row is not None

    def migrate(self) -> list[int]:
        """Apply every migration not yet applied, in order, in one transaction. Idempotent; returns the versions applied."""
        versions = [m.version for m in MIGRATIONS]
        if versions != sorted(set(versions)):
            raise RegistryError("the migration list is not strictly increasing", code="registry_error")
        applied_now: list[int] = []
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            done = {row["version"] for row in conn.execute("SELECT version FROM schema_version")}
            newest = max(versions)
            if done and max(done) > newest:
                raise RegistryError(
                    f"the registry database is at schema version {max(done)}, newer than this ToolForge knows ({newest})",
                    code="registry_too_new",
                    hint="upgrade ToolForge, or point TOOLFORGE_HOME at a different directory",
                )
            for migration in MIGRATIONS:
                if migration.version in done:
                    continue
                for step in migration.steps:
                    if callable(step):
                        step(conn)
                    else:
                        conn.execute(step)
                conn.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, self.now()),
                )
                applied_now.append(migration.version)
        return applied_now

    def schema_version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"] or 0) if row else 0


# --- Calls and statistics ------------------------------------------------------------------------------------------------

_TEXT_CAP = 80


def _clip(text: str | None) -> str | None:
    return None if text is None else "".join(c if c.isprintable() else "?" for c in text)[:_TEXT_CAP]


def record_call(
    db: Database,
    version_id: int,
    ok: bool,
    duration_ms: int,
    error_class: str | None = None,
    session_id: str | None = None,
) -> None:
    """Record one finished call. Free text is clipped, because error classes and session ids can originate from a tool run."""
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise RegistryError(
            f"duration_ms must be a non-negative whole number, got {duration_ms!r}", code="invalid_call"
        )
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM versions WHERE id = ?", (version_id,)).fetchone() is None:
            raise RegistryError(f"no version with id {version_id}", code="unknown_version")
        conn.execute(
            "INSERT INTO calls (version_id, ok, duration_ms, error_class, session_id, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (
                version_id,
                1 if ok else 0,
                duration_ms,
                _clip(error_class),
                _clip(session_id),
                db.now(),
            ),
        )


def call_stats(db: Database, version_id: int) -> Stats:
    """Uses (all calls) and the success rate over the last STATS_WINDOW calls; None until STATS_MIN_CALLS calls exist."""
    total = db.query_one("SELECT COUNT(*) AS n FROM calls WHERE version_id = ?", (version_id,))
    uses = int(total["n"]) if total else 0
    recent = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(ok), 0) AS good FROM "
        "(SELECT ok FROM calls WHERE version_id = ? ORDER BY id DESC LIMIT ?)",
        (version_id, STATS_WINDOW),
    )
    count, good = (int(recent["n"]), int(recent["good"])) if recent else (0, 0)
    rate = good / count if count >= STATS_MIN_CALLS else None
    return Stats(uses=uses, recent_calls=count, success_rate=rate, recent_ok=good)
