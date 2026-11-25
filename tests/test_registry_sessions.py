"""The sessions table (P13): ids, what is stored, time limits, orphans, and a second close."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest

from toolforge.errors import RegistryError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord
from toolforge.registry import sessions as sessions_module
from toolforge.registry.sessions import ORPHAN_AFTER_S, new_session_id, secret_matches, split_session_id

T0 = 1_000_000.0


@pytest.fixture
def version(registry: Registry, make_package: Callable[..., ToolPackage]) -> VersionRecord:
    return registry.add_version(make_package(), created_by="test")


def open_one(
    registry: Registry, version: VersionRecord, *, now: float = T0, ttl_s: int = 900, max_s: int = 3600
):
    session_id, public, digest = new_session_id()
    row = registry.open_session(
        public_id=public,
        secret_sha256=digest,
        version_id=version.id,
        label="demo",
        owner="owner-1",
        box="box-1",
        now=now,
        ttl_s=ttl_s,
        max_s=max_s,
    )
    return session_id, row


def test_an_id_has_a_public_half_and_a_secret_half_and_only_the_hash_of_the_secret_is_kept() -> None:
    session_id, public, digest = new_session_id()
    parts = split_session_id(session_id)
    assert parts is not None and parts[0] == public and public.startswith("s-") and len(public) == 10
    assert digest == hashlib.sha256(parts[1].encode()).hexdigest() and parts[1] not in digest
    assert secret_matches(digest, parts[1]) and not secret_matches(digest, "0" * 32)


def test_ids_are_not_repeated() -> None:
    assert len({new_session_id()[0] for _ in range(200)}) == 200


@pytest.mark.parametrize(
    "value",
    [
        None,
        7,
        "",
        "s-abcd1234",
        "s-abcd1234.",
        "s-abcd1234." + "0" * 31,
        "s-abcd1234." + "g" * 32,
        "x-abcd1234." + "0" * 32,
        "s-ABCD1234." + "0" * 32,
        "s-abcd1234-" + "0" * 32,
    ],
)
def test_a_value_that_is_not_shaped_like_an_id_is_rejected(value: object) -> None:
    assert split_session_id(value) is None


def test_opening_stores_the_row_with_both_time_limits(registry: Registry, version: VersionRecord) -> None:
    _, row = open_one(registry, version)
    assert (
        row.is_open
        and row.state == "open"
        and row.steps == 0
        and row.rewinds == 0
        and not row.close_requested
    )
    assert row.expires_at == T0 + 900 and row.hard_expires_at == T0 + 3600 and row.closed_at is None
    assert registry.get_session(row.public_id) == row


def test_the_idle_limit_never_goes_past_the_hard_limit(registry: Registry, version: VersionRecord) -> None:
    _, row = open_one(registry, version, ttl_s=900, max_s=600)
    assert row.expires_at == T0 + 600


def test_a_session_of_an_unknown_version_is_refused(registry: Registry) -> None:
    _, public, digest = new_session_id()
    with pytest.raises(RegistryError) as caught:
        registry.open_session(
            public_id=public,
            secret_sha256=digest,
            version_id=999,
            label="x",
            owner="o",
            box=None,
            now=T0,
            ttl_s=1,
            max_s=2,
        )
    assert caught.value.code == "unknown_version"


def test_touch_restarts_the_idle_clock_but_not_past_the_hard_limit(
    registry: Registry, version: VersionRecord
) -> None:
    _, row = open_one(registry, version)
    registry.touch_session(row.public_id, now=T0 + 100, ttl_s=900, box="box-2", steps=1)
    now = registry.get_session(row.public_id)
    assert (
        now.last_used_at == T0 + 100 and now.expires_at == T0 + 1000 and now.box == "box-2" and now.steps == 1
    )
    registry.touch_session(row.public_id, now=T0 + 3500, ttl_s=900)
    late = registry.get_session(row.public_id)
    assert late.expires_at == T0 + 3600 and late.box == "box-2" and late.steps == 1  # unset fields are kept


def test_due_says_why_a_session_should_be_closed(registry: Registry, version: VersionRecord) -> None:
    _, row = open_one(registry, version)
    assert row.due(T0 + 899) is None
    assert row.due(T0 + 900) == "idle"
    assert row.due(T0 + 3600) == "max_lifetime"  # the hard limit wins when both have passed


def test_a_session_is_orphaned_when_its_owner_stops_reporting(
    registry: Registry, version: VersionRecord
) -> None:
    _, row = open_one(registry, version)
    assert not row.orphaned(T0 + ORPHAN_AFTER_S) and row.orphaned(T0 + ORPHAN_AFTER_S + 1)
    assert registry.session_heartbeat("owner-1", now=T0 + 40) == 1
    assert not registry.get_session(row.public_id).orphaned(T0 + 80)
    assert registry.session_heartbeat("somebody-else", now=T0 + 40) == 0


def test_rewinds_are_counted_and_move_the_box(registry: Registry, version: VersionRecord) -> None:
    _, row = open_one(registry, version)
    registry.note_session_rewind(row.public_id, box="box-9", steps=2)
    registry.note_session_rewind(row.public_id, box="box-10", steps=1)
    now = registry.get_session(row.public_id)
    assert (now.rewinds, now.box, now.steps) == (2, "box-10", 1)


def test_a_close_request_only_marks_an_open_session(registry: Registry, version: VersionRecord) -> None:
    _, row = open_one(registry, version)
    assert registry.request_session_close(row.public_id) is True
    assert registry.get_session(row.public_id).close_requested and registry.get_session(row.public_id).is_open
    assert registry.request_session_close("s-00000000") is False
    registry.close_session(row.public_id, "closed", None, now=T0 + 1)
    assert registry.request_session_close(row.public_id) is False


def test_closing_keeps_the_row_and_a_second_close_changes_nothing(
    registry: Registry, version: VersionRecord
) -> None:
    _, row = open_one(registry, version)
    assert registry.close_session(row.public_id, "expired", "idle", now=T0 + 900) is True
    assert registry.close_session(row.public_id, "closed", "caller", now=T0 + 901) is False
    done = registry.get_session(row.public_id)
    assert (done.state, done.reason, done.closed_at, done.box) == ("expired", "idle", T0 + 900, None)
    assert not done.is_open and done.due(T0 + 9999) is None and not done.orphaned(T0 + 9999)


def test_a_state_that_is_not_a_closed_state_is_refused(registry: Registry, version: VersionRecord) -> None:
    _, row = open_one(registry, version)
    for bad in ("open", "nonsense"):
        with pytest.raises(RegistryError):
            registry.close_session(row.public_id, bad, None, now=T0)
    assert registry.get_session(row.public_id).is_open


def test_a_long_reason_is_clipped_and_cleaned(registry: Registry, version: VersionRecord) -> None:
    _, row = open_one(registry, version)
    registry.close_session(row.public_id, "lost", "bad\x1b[31m" + "x" * 500, now=T0)
    reason = registry.get_session(row.public_id).reason
    assert reason is not None and len(reason) == 200 and "\x1b" not in reason


def test_counting_and_listing(registry: Registry, version: VersionRecord) -> None:
    ids = [open_one(registry, version, now=T0 + n)[1].public_id for n in range(3)]
    assert registry.count_open_sessions() == 3
    registry.close_session(ids[0], "closed", None, now=T0 + 10)
    assert registry.count_open_sessions() == 2
    assert [r.public_id for r in registry.list_sessions()] == [ids[2], ids[1], ids[0]]  # newest first
    assert [r.public_id for r in registry.list_sessions(state="closed")] == [ids[0]]
    assert len(registry.list_sessions(limit=1)) == 1
    assert {r.public_id for r in registry.open_sessions_of(version.id)} == {ids[1], ids[2]}
    with pytest.raises(RegistryError):
        registry.list_sessions(state="bogus")


def test_the_secret_never_appears_in_a_row(registry: Registry, version: VersionRecord) -> None:
    session_id, row = open_one(registry, version)
    secret = session_id.split(".")[1]
    assert secret not in repr(registry.get_session(row.public_id)) and secret not in repr(
        registry.list_sessions()
    )


def test_the_module_exports_what_the_registry_uses() -> None:
    assert set(sessions_module.__all__) >= {
        "SessionRow",
        "new_session_id",
        "split_session_id",
        "secret_matches",
    }
