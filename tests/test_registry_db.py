"""The database layer: migrations, pragmas, transactions, savepoints, and concurrent writers."""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from toolforge.errors import RegistryError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle
from toolforge.registry import db as db_module
from toolforge.registry.db import MIGRATIONS, Database, Migration, utc_now

TABLES = {
    "tools",
    "versions",
    "gate_results",
    "calls",
    "approvals",
    "schema_version",
    "regression_cases",
    "problem_reports",
    "repairs",
}


def _tables(db: Database) -> set[str]:
    rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'tool_search%'")
    return {row["name"] for row in rows} - {"sqlite_sequence"}


def test_migration_list_is_ordered_and_unique() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(set(versions)) and versions[0] == 1


def test_fresh_database_has_every_table_and_the_search_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.sqlite3")
    assert _tables(db) == TABLES
    assert db.fts_available
    assert db.schema_version() == MIGRATIONS[-1].version
    db.close()


def test_opening_twice_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite3"
    first = Database(path)
    assert first.migrate() == []  # nothing left to apply
    first.close()
    second = Database(path)
    rows = second.query("SELECT version, name FROM schema_version ORDER BY version")
    assert [r["version"] for r in rows] == [m.version for m in MIGRATIONS]
    second.close()


def test_a_new_migration_is_applied_once_and_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "r.sqlite3"
    Database(path).close()
    later = Migration(
        MIGRATIONS[-1].version + 1,
        "a later table",
        (
            "CREATE TABLE later_table (id INTEGER PRIMARY KEY)",
            "CREATE INDEX later_by_id ON later_table(id)",
        ),
    )
    monkeypatch.setattr(db_module, "MIGRATIONS", [*MIGRATIONS, later])
    db = Database(path)
    assert "later_table" in _tables(db)
    assert db.schema_version() == later.version
    assert db.migrate() == []  # and not again
    db.close()


def test_a_failing_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "r.sqlite3"
    Database(path).close()
    broken = Migration(
        MIGRATIONS[-1].version + 1, "broken", ("CREATE TABLE half_done (id INTEGER)", "NOT SQL")
    )
    monkeypatch.setattr(db_module, "MIGRATIONS", [*MIGRATIONS, broken])
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    monkeypatch.undo()
    db = Database(path)
    assert "half_done" not in _tables(db)
    assert db.schema_version() == MIGRATIONS[-1].version
    db.close()


def test_a_database_from_a_newer_toolforge_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite3"
    db = Database(path)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO schema_version VALUES (?, 'from the future', ?)",
            (MIGRATIONS[-1].version + 5, utc_now()),
        )
    db.close()
    with pytest.raises(RegistryError) as caught:
        Database(path)
    assert caught.value.code == "registry_too_new"


def test_wal_mode_busy_timeout_and_foreign_keys(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.sqlite3")
    assert db.query_one("PRAGMA journal_mode")[0] == "wal"
    assert db.query_one("PRAGMA busy_timeout")[0] == db_module.BUSY_TIMEOUT_MS
    assert db.query_one("PRAGMA foreign_keys")[0] == 1
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("INSERT INTO calls (version_id, ok, duration_ms, ts) VALUES (999, 1, 1, 'x')")
    db.close()


def test_reads_outside_a_transaction_cannot_write(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        db.query("INSERT INTO tools VALUES ('x', NULL, 'now')")
    db.close()


def test_timestamps_are_utc_iso_strings(registry: Registry, make_package: Callable[..., ToolPackage]) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
    assert re.fullmatch(pattern, record.created_at) and re.fullmatch(pattern, record.updated_at)


def test_a_transaction_commits_and_a_raise_rolls_back(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.sqlite3")
    with db.transaction() as conn:
        conn.execute("INSERT INTO tools VALUES ('kept', NULL, 'now')")
    with pytest.raises(ValueError), db.transaction() as conn:
        conn.execute("INSERT INTO tools VALUES ('lost', NULL, 'now')")
        raise ValueError("boom")
    assert [r["name"] for r in db.query("SELECT name FROM tools")] == ["kept"]
    db.close()


def test_a_nested_failure_undoes_only_the_inner_work(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.sqlite3")
    with db.transaction() as conn:
        conn.execute("INSERT INTO tools VALUES ('outer', NULL, 'now')")
        with pytest.raises(ValueError), db.transaction() as inner:
            inner.execute("INSERT INTO tools VALUES ('inner', NULL, 'now')")
            raise ValueError("boom")
        assert [r["name"] for r in db.query("SELECT name FROM tools")] == [
            "outer"
        ]  # a read inside sees its own writes
    assert [r["name"] for r in db.query("SELECT name FROM tools")] == ["outer"]
    db.close()


def test_other_threads_do_not_see_uncommitted_changes(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.sqlite3")
    seen: list[int] = []
    with db.transaction() as conn:
        conn.execute("INSERT INTO tools VALUES ('pending', NULL, 'now')")
        thread = threading.Thread(target=lambda: seen.append(len(db.query("SELECT * FROM tools"))))
        thread.start()
        thread.join()
    assert seen == [0]
    assert len(db.query("SELECT * FROM tools")) == 1
    db.close()


def test_the_lock_is_shared_by_every_database_on_one_file(tmp_path: Path) -> None:
    first, second = Database(tmp_path / "r.sqlite3"), Database(tmp_path / "r.sqlite3")
    assert first._lock is second._lock
    first.close()
    second.close()


def test_a_closed_database_refuses_use(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.sqlite3")
    db.close()
    db.close()  # twice is fine
    with pytest.raises(RegistryError):
        db.query("SELECT 1")


def test_concurrent_writers_do_not_corrupt_state(
    tmp_path: Path, make_package: Callable[..., ToolPackage]
) -> None:
    """Threads add versions, record calls and promote at once; every row lands exactly once and the invariants hold."""
    registry = Registry(tmp_path / "home")
    threads_n, calls_each = 8, 25
    shared = registry.add_version(make_package("shared_tool", "1.0.0"), created_by="agent")
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            own = registry.add_version(make_package(f"tool_{index:02d}", "1.0.0"), created_by="agent")
            registry.transition(own.id, "verifying", actor="pipeline")
            registry.transition(own.id, "verified", actor="pipeline")
            registry.transition(own.id, "active", actor="pipeline")
            for n in range(calls_each):
                registry.record_call(shared.id, ok=n % 5 != 0, duration_ms=n)
                registry.record_call(own.id, ok=True, duration_ms=1)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    assert not errors
    assert registry.stats(shared.id).uses == threads_n * calls_each
    assert len(registry.list_tools()) == threads_n + 1
    assert len(registry.list_versions(status="active")) == threads_n
    assert registry.db.query_one("PRAGMA integrity_check")[0] == "ok"
    assert (
        len(registry.search("count words", limit=50)) == threads_n
    )  # every active tool is findable, once each
    registry.close()


def test_concurrent_promotions_of_one_tool_leave_exactly_one_active(
    tmp_path: Path, make_package: Callable[..., ToolPackage]
) -> None:
    registry = Registry(tmp_path / "home")
    versions = [
        registry.add_version(make_package("busy_tool", f"1.0.{n}"), created_by="agent") for n in range(6)
    ]
    for record in versions:
        registry.transition(record.id, "verifying", actor="pipeline")
        registry.transition(record.id, "verified", actor="pipeline")
    errors: list[BaseException] = []

    def promote(version_id: int) -> None:
        try:
            registry.transition(version_id, "active", actor="pipeline")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=promote, args=(r.id,)) for r in versions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    active = registry.list_versions(name="busy_tool", status="active")
    assert len(active) == 1
    assert registry.active_version("busy_tool") == active[0]
    assert len(registry.search("count words")) == 1
    assert sum(1 for r in registry.list_versions(name="busy_tool") if r.status == lifecycle.DEPRECATED) == 5
    registry.close()
