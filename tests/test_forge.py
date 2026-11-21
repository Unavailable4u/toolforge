"""`Forge.submit` with scripted gates and a real registry: what is registered, what is returned again, what is refused, how many run at once."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from verify_helpers import MemoryLedgers, ScriptedGate, valid_package

from toolforge.config import Settings
from toolforge.errors import RegistryError, SandboxUnavailable
from toolforge.registry import Registry, lifecycle
from toolforge.services.forge import Forge
from toolforge.verify.pipeline import failed

ALL = ("G1", "G2", "G3", "G4")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def make(settings, registry, gate_list=None, ledgers=None) -> tuple[Forge, list[ScriptedGate]]:
    gate_list = gate_list if gate_list is not None else [ScriptedGate(g) for g in ALL]
    return Forge(settings, registry, None, ledger_factory=ledgers, gates=gate_list), gate_list


def payload_of(package) -> dict:
    return {"manifest": package.manifest.model_dump(mode="json"), "files": dict(package.files)}


# --- Submitting ------------------------------------------------------------------------------------------------------------------


def test_a_good_submission_is_registered_and_verified(settings, registry) -> None:
    forge, gates = make(settings, registry)
    report = forge.submit(payload_of(valid_package()), "agent-1")
    assert report.status == lifecycle.VERIFIED_BASIC and report.registered and not report.dry_run
    record = registry.find_version("my_tool", "0.1.0")
    assert record.status == lifecycle.VERIFIED_BASIC and record.created_by == "agent-1"
    assert [g.calls for g in gates] == [1, 1, 1, 1]


def test_a_payload_that_is_not_a_package_is_rejected_and_nothing_is_registered(settings, registry) -> None:
    forge, _ = make(
        settings,
        registry,
        [ScriptedGate("G1", failed("the submission is invalid", ["send manifest and files"]))],
    )
    report = forge.submit({"manifest": "not an object", "files": {}}, "agent")
    assert report.status == "rejected" and not report.registered
    assert registry.list_versions() == []


def test_a_blank_submitter_is_recorded_as_unknown_and_a_long_one_is_clipped(settings, registry) -> None:
    forge, _ = make(settings, registry)
    forge.submit_package(valid_package(name="tool_a"), "   ")
    forge.submit_package(valid_package(name="tool_b"), "x" * 500)
    assert registry.find_version("tool_a", "0.1.0").created_by == "unknown"
    assert len(registry.find_version("tool_b", "0.1.0").created_by) <= 80


def test_an_invalid_package_is_rejected_and_not_registered(settings, registry) -> None:
    forge, _ = make(
        settings,
        registry,
        [ScriptedGate("G1", failed("the manifest is invalid", ["fix"])), ScriptedGate("G2")],
    )
    report = forge.submit_package(valid_package(description="short"), "agent")
    assert report.status == "rejected" and not report.registered
    assert registry.list_versions() == []


def test_a_rejected_package_is_kept_and_its_version_is_used_up(settings, registry) -> None:
    gates = [ScriptedGate("G1"), ScriptedGate("G2", failed("the build failed", ["pin it"]))]
    forge, _ = make(settings, registry, gates)
    report = forge.submit_package(valid_package(), "agent")
    assert (
        report.status == "rejected" and registry.find_version("my_tool", "0.1.0").status == lifecycle.REJECTED
    )
    again = forge.submit_package(valid_package(), "agent")  # the same content: the stored verdict, no new run
    assert again.status == "rejected" and gates[0].calls == 1


# --- The same name and version ---------------------------------------------------------------------------------------------------


def test_the_same_content_returns_the_stored_report_without_running_again(settings, registry) -> None:
    forge, gates = make(settings, registry)
    first = forge.submit_package(valid_package(), "agent")
    second = forge.submit_package(valid_package(), "someone-else")
    assert second.status == first.status and second.version_id == first.version_id
    assert [g.status for g in second.gates] == [g.status for g in first.gates]
    assert [g.calls for g in gates] == [1, 1, 1, 1]
    assert len(registry.list_versions()) == 1


def test_different_content_under_the_same_version_is_refused_and_leaves_the_first_alone(
    settings, registry
) -> None:
    forge, gates = make(settings, registry)
    forge.submit_package(valid_package(), "agent")
    changed = valid_package(tool="def run(args):\n    return {'text': 'changed', 'words': 0}\n")
    with pytest.raises(RegistryError) as raised:
        forge.submit_package(changed, "agent")
    assert raised.value.code == "version_exists" and "bump" in raised.value.hint
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.VERIFIED_BASIC
    assert [g.calls for g in gates] == [1, 1, 1, 1]


def test_a_new_version_of_the_same_tool_is_its_own_run(settings, registry) -> None:
    forge, gates = make(settings, registry)
    forge.submit_package(valid_package(), "agent")
    report = forge.submit_package(valid_package(version="0.2.0", parent_version="0.1.0"), "agent")
    assert report.status == lifecycle.VERIFIED_BASIC and [g.calls for g in gates] == [2, 2, 2, 2]


def test_a_version_stuck_in_verifying_is_resumed_by_submitting_the_same_content(settings, registry) -> None:
    ledgers = MemoryLedgers()
    boom = SandboxUnavailable("no docker", code="docker_unavailable", hint="start it")
    broken, _ = make(settings, registry, [ScriptedGate("G1"), ScriptedGate("G2", raises=boom)], ledgers)
    package = valid_package()
    with pytest.raises(SandboxUnavailable):
        broken.submit_package(package, "agent")
    assert registry.find_version("my_tool", "0.1.0").status == lifecycle.VERIFYING
    working, gates = make(settings, registry, ledgers=ledgers)
    report = working.submit_package(package, "agent")
    assert report.status == lifecycle.VERIFIED_BASIC and len(registry.list_versions()) == 1
    assert [g.calls for g in gates] == [1, 1, 1, 1]  # a resumed run starts the gates again from G1


def test_different_content_for_a_version_stuck_in_verifying_is_still_refused(settings, registry) -> None:
    boom = SandboxUnavailable("no docker", code="docker_unavailable", hint="start it")
    broken, _ = make(settings, registry, [ScriptedGate("G1"), ScriptedGate("G2", raises=boom)])
    with pytest.raises(SandboxUnavailable):
        broken.submit_package(valid_package(), "agent")
    working, _ = make(settings, registry)
    with pytest.raises(RegistryError) as raised:
        working.submit_package(
            valid_package(tool="def run(args):\n    return {'text': '', 'words': 1}\n"), "agent"
        )
    assert raised.value.code == "version_exists"


# --- Concurrency -----------------------------------------------------------------------------------------------------------------


def test_no_more_runs_than_the_limit_are_in_flight_at_once(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home", max_concurrent_verifications=2)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}
    release = threading.Event()

    def hold(_ctx) -> None:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        release.wait(timeout=1.0)
        with lock:
            state["now"] -= 1

    results: list[str] = []
    with Registry(settings.home) as registry:
        forge = Forge(settings, registry, None, gates=[ScriptedGate("G1", action=hold)])

        def submit(n: int) -> None:
            report = forge.submit_package(valid_package(name=f"tool_{n}"), "agent")
            with lock:
                results.append(report.status)

        threads = [threading.Thread(target=submit, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        release.wait(timeout=0.3)
        release.set()
        for t in threads:
            t.join(timeout=10)
    assert state["peak"] <= 2
    assert sorted(results) == [lifecycle.VERIFIED_BASIC] * 5


# --- P07: the new-version rule and the queue timeout -------------------------------------------------------------------------------------


def test_a_new_version_of_an_active_tool_must_name_it_as_its_parent(settings, registry) -> None:
    forge, _ = make(settings, registry)
    first = valid_package()
    forge.submit_package(first, "agent")
    record = registry.find_version("my_tool", "0.1.0")
    registry.transition(record.id, lifecycle.ACTIVE, actor="operator", reason="test")
    for parent in (None, "0.0.9"):
        with pytest.raises(RegistryError) as caught:
            forge.submit_package(valid_package(version="0.2.0", parent_version=parent), "agent")
        assert caught.value.code == "name_collision" and "0.1.0" in str(caught.value)
    assert registry.find_version("my_tool", "0.2.0") is None  # refused before anything was registered
    report = forge.submit_package(valid_package(version="0.2.0", parent_version="0.1.0"), "agent")
    assert report.registered


def test_a_tool_that_is_not_active_has_no_parent_rule(settings, registry) -> None:
    forge, _ = make(settings, registry)
    forge.submit_package(valid_package(), "agent")  # verified_basic, not active
    assert forge.submit_package(valid_package(version="0.2.0"), "agent").registered


def test_a_submission_that_waits_too_long_for_a_slot_gets_busy(registry, tmp_path) -> None:
    from toolforge.errors import Busy

    release, started = threading.Event(), threading.Event()

    def hold(ctx) -> None:
        started.set()
        release.wait(10)

    one_slot = Settings(home=tmp_path / "home", max_concurrent_verifications=1)
    forge = Forge(
        one_slot,
        registry,
        None,
        gates=[ScriptedGate("G1", action=hold), *[ScriptedGate(g) for g in ("G2", "G3", "G4")]],
        queue_timeout_s=0.1,
    )
    worker = threading.Thread(target=lambda: forge.submit_package(valid_package(name="slow_tool"), "a"))
    worker.start()
    try:
        assert started.wait(10)
        with pytest.raises(Busy) as caught:
            forge.submit_package(valid_package(name="late_tool"), "b")
        assert caught.value.code == "busy" and "again" in str(caught.value)
        assert registry.find_version("late_tool", "0.1.0") is None
    finally:
        release.set()
        worker.join(10)
