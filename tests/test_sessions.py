"""Sessions (P13): `SessionManager` with the real harness and a stand-in sandbox that is not isolated (trusted test code only).

What a real container adds (snapshots of a live container, the egress rule, the reaper's containers) is `tests/docker/test_session_containers.py`.
Test names that `services/sessions.py` promises are kept exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sessions_helpers import SessionRig, counter_package, posix_only

from toolforge.errors import Busy, ToolNotRunnable
from toolforge.registry import sessions as store
from toolforge.services import sessions as sessions_module

pytestmark = posix_only


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    made = SessionRig(tmp_path, monkeypatch)
    yield made
    made.close()


def events(rig: SessionRig, kind: str) -> list[dict]:
    return [payload for _, actor, typ, payload in rig.ledgers.events if typ == kind]


def test_a_counter_returns_1_2_3_a_new_session_starts_at_1_and_rewind_continues_from_the_step(rig) -> None:
    record = rig.add()
    first = rig.call(record)
    assert first.ok and first.result == {"n": 1} and first.step == 1 and first.session_id
    sid = first.session_id
    assert [rig.call(record, session_id=sid).result["n"] for _ in range(2)] == [2, 3]
    other = rig.call(record)
    assert other.result == {"n": 1} and other.session_id != sid
    assert rig.sessions.rewind(sid, 1)["step"] == 1
    again = rig.call(record, session_id=sid)
    assert again.result == {"n": 2} and again.step == 2


def test_the_session_keeps_one_sandbox_and_the_manifest_network_rule(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    rig.call(record, session_id=sid)
    assert len(rig.sandbox.handles) == 1
    spec = rig.sandbox.specs[0]
    assert spec.network == "none" and spec.decoys is True and spec.label == f"session:{record.label}"


def test_a_failed_call_is_not_a_step_and_does_not_change_the_state(rig) -> None:
    record = rig.add()
    sid = rig.call(record, {"by": 2}).session_id
    failed = rig.call(record, {"by": -1}, sid)
    assert not failed.ok and failed.error.type == "ToolError" and failed.step == 1
    crashed = rig.call(record, {"boom": True}, sid)
    assert not crashed.ok and crashed.error.type == "Crash"
    assert rig.call(record, {"by": 1}, sid).result == {"n": 3}


def test_a_first_call_that_fails_leaves_no_session_behind(rig) -> None:
    record = rig.add()
    result = rig.call(record, {"by": -1})
    assert not result.ok and result.session_id is None
    (row,) = rig.registry.list_sessions()
    assert (row.state, row.reason) == ("closed", "open_failed")
    assert rig.sandbox.handles[0].closed


def test_state_over_256_kb_fails_the_call_and_is_not_saved(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    big = rig.call(record, {"big": True}, sid)
    assert not big.ok and big.error.type == "OutputTooLarge" and "256 KB" in big.error.message
    assert rig.call(record, {}, sid).result == {"n": 2}


def test_state_that_is_not_json_fails_the_call_and_is_not_saved(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    bad = rig.call(record, {"bad": True}, sid)
    assert not bad.ok and bad.error.type == "NotJson"
    assert rig.call(record, {}, sid).result == {"n": 2}


def test_the_package_and_harness_are_restored_before_every_call(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    tampered = rig.call(record, {"tamper": True}, sid)
    assert tampered.ok and tampered.result == {"n": 2}
    after = rig.call(record, {}, sid)  # the tool overwrote its own code and the harness; both came back
    assert after.ok and after.result == {"n": 3}


def test_idle_and_max_lifetime_expiry(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    rig.clock.advance(899)
    rig.sessions.sweep()
    assert rig.call(record, session_id=sid).ok  # still open, and the call restarted the idle clock
    rig.clock.advance(901)
    rig.sessions.sweep()
    (row,) = rig.registry.list_sessions()
    assert (row.state, row.reason) == ("expired", "idle")
    with pytest.raises(ToolNotRunnable) as info:
        rig.call(record, session_id=sid)
    assert info.value.code == "session_ended" and "idle" in str(info.value)
    assert rig.sandbox.handles[0].closed
    long = rig.call(record).session_id
    for _ in range(7):  # one call every 600 s keeps it idle-fresh, but not past the hour
        rig.clock.advance(600)
        rig.sessions.sweep()
        try:
            assert rig.call(record, session_id=long).ok
        except ToolNotRunnable as exc:
            assert exc.code == "session_ended"
            break
    newest = rig.registry.get_session(store.split_session_id(long)[0])
    assert newest.state == "expired" and newest.reason == "max_lifetime"


def test_max_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = SessionRig(tmp_path, monkeypatch, max_sessions=2)
    try:
        record = rig.add()
        first, second = rig.call(record).session_id, rig.call(record).session_id
        with pytest.raises(Busy) as info:
            rig.call(record)
        assert "2 sessions are already open" in str(info.value) and "forge_session_close" in (
            info.value.hint or ""
        )
        assert rig.clock.now >= 10_000 + sessions_module.OPEN_WAIT_S  # it waited, with the injected sleep
        rig.sessions.close(first)
        assert rig.call(record).ok
        assert second
    finally:
        rig.close()


def test_a_caller_cannot_use_another_callers_session(rig) -> None:
    record = rig.add()
    mine = rig.call(record).session_id
    public, secret = mine.split(".")
    guesses = [f"{public}.{'0' * 32}", f"s-00000000.{secret}", "nonsense", "", 5, f"{public}.{secret[:-1]}"]
    messages = set()
    for guess in guesses:
        for act in (
            lambda g=guess: rig.call(record, session_id=g),
            lambda g=guess: rig.sessions.rewind(g, 0),
            lambda g=guess: rig.sessions.close(g),
            lambda g=guess: rig.sessions.describe(g),
        ):
            with pytest.raises(ToolNotRunnable) as info:
                act()
            assert info.value.code == "bad_session"
            messages.add(str(info.value))
    assert (
        len(messages) == 1
    )  # an unknown id and a wrong secret get the same answer, and it never echoes the id
    assert rig.call(record, session_id=mine).ok  # none of that touched it
    listed = json.dumps(rig.sessions.describe())
    assert public in listed and secret not in listed


def test_a_session_id_does_not_work_on_another_tool(rig) -> None:
    one, two = rig.add(counter_package("counter")), rig.add(counter_package("tally"))
    sid = rig.call(one).session_id
    with pytest.raises(ToolNotRunnable) as info:
        rig.sessions.call(rig.runner._resolve(two, ("active",)), {}, None, sid)
    assert info.value.code == "session_other_tool"


def test_rewind_restores_the_right_snapshot(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    for _ in range(5):
        rig.call(record, session_id=sid)  # steps 2..6, totals 2..6
    available = rig.sessions.describe(sid)["session"]["steps_available"]
    assert available == [2, 3, 4, 5, 6]  # the last 5 are kept
    with pytest.raises(ToolNotRunnable) as info:
        rig.sessions.rewind(sid, 1)
    assert info.value.code == "step_not_available" and "[2, 3, 4, 5, 6]" in (info.value.hint or "")
    moved = rig.sessions.rewind(sid, 3)
    assert moved["step"] == 3 and moved["from_step"] == 6 and moved["steps_available"] == [2, 3]
    assert rig.call(record, session_id=sid).result == {"n": 4}  # continued from step 3
    assert len(rig.sandbox.handles) == 2 and rig.sandbox.handles[0].closed
    kinds = [t for _, a, t, p in rig.ledgers.events if t.startswith("forge.session.")]
    assert (
        kinds.count("forge.session.rewound") == 1 and kinds.count("forge.session.step") == 7
    )  # the history stays
    with pytest.raises(ToolNotRunnable) as bad:
        rig.sessions.rewind(sid, "3")
    assert bad.value.code == "bad_step"


def test_a_snapshot_too_large_to_keep_cannot_be_rewound_to(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    monkeypatch.setattr(sessions_module, "MAX_SNAPSHOT_BYTES", 10)
    rig.call(record, session_id=sid)
    with pytest.raises(ToolNotRunnable) as info:
        rig.sessions.rewind(sid, 2)
    assert info.value.code == "step_not_available"


def test_a_timeout_restores_the_last_step(rig) -> None:
    record = rig.add()
    sid = rig.call(record, {"by": 3}).session_id
    rig.sandbox.kill_on_call = rig.sandbox.calls + 1
    killed = rig.call(record, {"by": 1}, sid)
    assert not killed.ok and killed.error.type == "Timeout"
    assert "restored to step 1" in killed.error.hint and killed.step == 1
    assert len(rig.sandbox.handles) == 2
    assert rig.call(record, {"by": 1}, sid).result == {
        "n": 4
    }  # the state from before the dead call, in a new sandbox
    assert events(rig, "forge.session.recovered")


def test_a_session_whose_sandbox_cannot_be_restored_is_closed_as_lost(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    rig.sandbox.kill_on_call = rig.sandbox.calls + 1
    rig.sandbox.fail_open_after = 1
    result = rig.call(record, {}, sid)
    assert not result.ok and result.session_id is None
    assert rig.registry.list_sessions()[0].state == "lost"
    with pytest.raises(ToolNotRunnable):
        rig.call(record, {}, sid)


def test_closing_audits_once_and_says_so_twice(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    done = rig.sessions.close(sid)
    assert (
        done["closed"]
        and done["steps"] == 1
        and done["real_secret_audit"] == "not_measured"
        and done["cleanup_ok"]
    )
    again = rig.sessions.close(sid)
    assert again["already_closed"] and again["state"] == "closed"
    finished = [p for _, a, t, p in rig.ledgers.events if t == "forge.call.finished"]
    assert len(finished) == 1 and finished[0]["why"] == "session_end"
    assert len(events(rig, "forge.session.closed")) == 1 and rig.sandbox.closed_boxes == 1
    assert rig.sessions.live_count() == 0


def test_quarantine_closes_sessions_and_deprecation_does_not(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    newer = rig.add(counter_package(version="0.2.0"))  # promoting it deprecates the first
    assert rig.registry.get_version(record.id).status == "deprecated"
    assert rig.call(record, {}, sid).result == {"n": 2}  # the session lives on, on the version it opened with
    on_newer = rig.call(newer).session_id
    rig.registry.transition(newer.id, "quarantined", actor="operator", reason="test")
    rig.sessions.sweep()
    states = {r.public_id: (r.state, r.reason) for r in rig.registry.list_sessions()}
    assert states[store.split_session_id(on_newer)[0]] == ("quarantined", "quarantined")
    assert states[store.split_session_id(sid)[0]][0] == "open"  # a deprecated version's session is untouched
    with pytest.raises(ToolNotRunnable) as info:
        rig.call(newer, {}, on_newer)
    assert info.value.code == "session_ended" and "quarantined" in str(info.value)


def test_a_call_notices_a_quarantine_before_the_reaper_does(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    rig.registry.transition(record.id, "quarantined", actor="operator", reason="test")
    with pytest.raises(ToolNotRunnable) as info:
        rig.call(record, {}, sid)
    assert info.value.code == "session_ended"
    assert rig.registry.list_sessions()[0].state == "quarantined" and rig.sandbox.handles[0].closed


def test_the_decoy_tripwire_quarantines_the_version_and_ends_its_sessions(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    other = rig.call(record).session_id
    rig.sandbox.touch_decoy_on_call = rig.sandbox.calls + 1
    result = rig.call(record, {}, sid)
    assert not result.ok and result.error.type == "PolicyViolation" and result.result is None
    assert rig.registry.get_version(record.id).status == "quarantined"
    states = {r.public_id: r.state for r in rig.registry.list_sessions()}
    assert set(states.values()) == {"quarantined"} and len(states) == 2
    with pytest.raises(ToolNotRunnable):
        rig.call(record, {}, other)


def test_the_operator_can_ask_for_a_close_and_the_reaper_does_it(rig) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    assert rig.registry.request_session_close(store.split_session_id(sid)[0])
    rig.sessions.sweep()
    row = rig.registry.list_sessions()[0]
    assert (row.state, row.reason) == ("operator", "operator")


def test_the_session_ledger_records_hashes_and_sizes_never_arguments_or_results(rig) -> None:
    record = rig.add()
    sid = rig.call(record, {"by": 4}).session_id
    rig.call(record, {"by": 3}, sid)
    rig.sessions.close(sid)
    kinds = [t for _, a, t, p in rig.ledgers.events if t.startswith("forge.session.")]
    assert kinds == [
        "forge.session.opened",
        "forge.session.step",
        "forge.session.step",
        "forge.session.closed",
    ]
    step = events(rig, "forge.session.step")[1]
    assert step["step"] == 2 and step["ok"] and len(step["args_sha256"]) == 64 and step["state_bytes"] > 0
    text = json.dumps([p for *_, p in rig.ledgers.events])
    assert sid.split(".")[1] not in text  # the secret half of the id is never written anywhere
    paths = [p for p in rig.ledgers.paths if p]
    assert any("/sessions/" in p for p in paths) and any(p.endswith("-1.jsonl") for p in paths)


def test_a_session_survives_only_with_a_registered_version(rig) -> None:
    from toolforge.models.package import ToolPackage  # noqa: F401

    with pytest.raises(ToolNotRunnable) as info:
        rig.runner.run_unverified(counter_package(), {"by": 1})
    assert info.value.code == "session_needs_registry"


def test_shutdown_closes_every_session_this_process_holds(rig) -> None:
    record = rig.add()
    rig.call(record), rig.call(record)
    assert rig.sessions.shutdown() == 2
    assert {r.state for r in rig.registry.list_sessions()} == {"closed"}
    assert all(h.closed for h in rig.sandbox.handles)


def test_a_session_held_by_another_process_cannot_be_run_here(rig, tmp_path: Path) -> None:
    record = rig.add()
    sid = rig.call(record).session_id
    # A second runner on the same registry is "another process": the row is open, the sandbox is not its own.
    from toolforge.services.runner import ToolRunner

    second = ToolRunner(
        rig.sandbox,
        rig.registry,
        rig.settings,
        rig.ledgers,
        session_reaper=False,
        session_clock=rig.clock,
        session_sleep=rig.clock.advance,
    )
    with pytest.raises(ToolNotRunnable) as info:
        second.run(record, {}, session_id=sid)
    assert info.value.code == "session_elsewhere"
