"""Stateful sessions against a real Docker daemon (P13): `ToolRunner` -> `SessionManager` -> `MiniLockerRunner` -> `harness.py`.

Skipped automatically when no daemon answers (see tests/conftest.py). They show what the stand-in tests cannot: that a session keeps one
live container, that a snapshot of it can be restored into a new one (rewind), and that the reaper removes the container of an idle
session.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sessions_helpers import COUNTER_SCHEMA, counter_package

from toolforge.cli_run import make_ledger
from toolforge.config import Settings
from toolforge.registry import Registry, VersionRecord, lifecycle
from toolforge.sandbox.minilocker_runner import MiniLockerRunner
from toolforge.services.forge import Forge
from toolforge.services.runner import ToolRunner

pytestmark = [pytest.mark.docker, pytest.mark.usefixtures("minilocker")]

CLEAN_SCHEMA = {k: v for k, v in COUNTER_SCHEMA.items() if k != "properties"} | {
    "properties": {"by": COUNTER_SCHEMA["properties"]["by"]}
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home", session_ttl_s=3)


@pytest.fixture
def rig(settings: Settings) -> Iterator[tuple[ToolRunner, Registry, VersionRecord, MiniLockerRunner]]:
    sandbox = MiniLockerRunner(settings=settings)
    with Registry.from_settings(settings) as registry:
        forge = Forge(settings, registry, sandbox, ledger_factory=make_ledger)
        report = forge.submit_package(counter_package(input_schema=CLEAN_SCHEMA), "test")
        assert report.status == lifecycle.VERIFIED_BASIC, report.text()
        record = registry.find_version("counter", report.version)
        assert record is not None
        registry.transition(record.id, lifecycle.ACTIVE, actor="operator", reason="session container tests")
        record = registry.get_version(record.id)
        runner = ToolRunner(sandbox, registry, settings, make_ledger)
        yield runner, registry, record, sandbox
        runner.sessions.shutdown()
    sandbox.shutdown()


def test_a_counter_gives_1_2_3_in_one_session_and_a_new_session_starts_at_1(rig) -> None:
    runner, _, record, _ = rig
    first = runner.run(record, {"by": 1})
    assert first.ok and first.result == {"n": 1} and first.session_id
    assert [runner.run(record, {"by": 1}, session_id=first.session_id).result["n"] for _ in range(2)] == [
        2,
        3,
    ]
    assert runner.run(record, {"by": 1}).result == {"n": 1}


def test_a_rewind_restores_the_state_of_the_chosen_step_in_a_new_container(rig) -> None:
    runner, registry, record, _ = rig
    first = runner.run(record, {"by": 1})
    sid = first.session_id
    runner.run(record, {"by": 1}, session_id=sid)
    runner.run(record, {"by": 1}, session_id=sid)
    box_before = registry.get_session(sid.split(".")[0]).box
    runner.sessions.rewind(sid, 1)
    assert (
        registry.get_session(sid.split(".")[0]).box != box_before
    )  # a new container, not the old one edited
    assert runner.run(record, {"by": 1}, session_id=sid).result == {"n": 2}


def test_closing_a_session_removes_its_container(rig) -> None:
    import docker

    runner, registry, record, _ = rig
    first = runner.run(record, {"by": 1})
    public = first.session_id.split(".")[0]
    box = registry.get_session(public).box
    client = docker.from_env()
    assert [c.name for c in client.containers.list(filters={"name": box})]
    runner.sessions.close(first.session_id)
    assert not [c.name for c in client.containers.list(all=True, filters={"name": box})]


def test_an_idle_session_is_closed_and_its_container_removed_by_the_reaper(rig) -> None:
    import docker

    runner, registry, record, _ = rig
    first = runner.run(record, {"by": 1})
    public = first.session_id.split(".")[0]
    box = registry.get_session(public).box
    deadline = time.monotonic() + 30  # idle limit 3 s; the reaper looks every 5 s
    while time.monotonic() < deadline and registry.get_session(public).is_open:
        time.sleep(0.5)
    row = registry.get_session(public)
    assert row.state == "expired" and not [
        c.name for c in docker.from_env().containers.list(all=True, filters={"name": box})
    ]
