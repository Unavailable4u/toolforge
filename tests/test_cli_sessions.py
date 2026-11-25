"""`toolforge sessions list` and `toolforge sessions close` (P13), through the registry's session table."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from toolforge import cli_sessions
from toolforge.cli import main
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord
from toolforge.registry.sessions import new_session_id

Make = Callable[..., ToolPackage]


@pytest.fixture
def registry(tmp_home: Path) -> Iterator[Registry]:
    with Registry(tmp_home) as registry:
        yield registry


@pytest.fixture
def version(registry: Registry, make_package: Make) -> VersionRecord:
    return registry.add_version(make_package("counter", "0.1.0"), created_by="test")


def open_one(
    registry: Registry, version: VersionRecord, *, now: float | None = None, box: str | None = "tf-box-1"
) -> tuple[str, str]:
    session_id, public, digest = new_session_id()
    registry.open_session(
        public_id=public,
        secret_sha256=digest,
        version_id=version.id,
        label="counter 0.1.0",
        owner="owner-1",
        box=box,
        now=time.time() if now is None else now,
        ttl_s=900,
        max_s=3600,
    )
    return session_id, public


def test_list_with_no_sessions(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sessions", "list"]) == 0
    assert capsys.readouterr().out.strip() == "no sessions"


def test_list_shows_open_sessions_by_public_name_and_never_the_secret(
    registry: Registry, version: VersionRecord, capsys: pytest.CaptureFixture[str]
) -> None:
    session_id, public = open_one(registry, version)
    assert main(["sessions", "list"]) == 0
    out = capsys.readouterr().out
    assert public in out and "counter 0.1.0" in out and "running" in out
    assert session_id.split(".")[1] not in out


def test_list_hides_ended_sessions_unless_asked(
    registry: Registry, version: VersionRecord, capsys: pytest.CaptureFixture[str]
) -> None:
    _, public = open_one(registry, version)
    registry.close_session(public, "expired", "idle for 900 s", now=time.time())
    assert main(["sessions", "list"]) == 0
    assert capsys.readouterr().out.strip() == "no sessions"
    assert main(["sessions", "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert public in out and "expired" in out and "idle for 900 s" in out


def test_an_open_session_whose_server_stopped_reporting_is_shown_as_gone(
    registry: Registry, version: VersionRecord, capsys: pytest.CaptureFixture[str]
) -> None:
    open_one(registry, version, now=time.time() - 600)
    assert main(["sessions", "list"]) == 0
    assert "gone" in capsys.readouterr().out


def test_close_needs_exactly_one_target(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sessions", "close"]) == 2
    assert main(["sessions", "close", "s-00000000", "--all"]) == 2
    assert "bad_arguments" in capsys.readouterr().err


def test_closing_an_unknown_session_fails_plainly(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sessions", "close", "s-00000000"]) == 1
    assert "no session s-00000000" in capsys.readouterr().out


def test_closing_an_orphaned_session_removes_its_container_and_says_there_was_no_audit(
    registry: Registry,
    version: VersionRecord,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_id, public = open_one(registry, version, now=time.time() - 600, box="tf-gone-box")
    removed: list[str | None] = []
    monkeypatch.setattr(
        cli_sessions, "_remove_container", lambda settings, name: removed.append(name) or True
    )
    assert main(["sessions", "close", session_id]) == 0  # a full id is accepted; only its public half is used
    assert removed == ["tf-gone-box"] and "without an audit" in capsys.readouterr().out
    row = registry.get_session(public)
    assert row.state == "operator" and "no audit" in (row.reason or "")


def test_an_orphan_whose_container_cannot_be_removed_stays_open_and_the_command_fails(
    registry: Registry,
    version: VersionRecord,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, public = open_one(registry, version, now=time.time() - 600)
    monkeypatch.setattr(cli_sessions, "_remove_container", lambda settings, name: False)
    assert main(["sessions", "close", public]) == 1
    assert "docker rm -f tf-box-1" in capsys.readouterr().out
    assert registry.get_session(public).is_open


def test_closing_a_live_session_asks_its_server_and_waits_for_it(
    registry: Registry,
    version: VersionRecord,
    monkeypatch: pytest.MonkeyPatch,
    tmp_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, public = open_one(registry, version)
    monkeypatch.setattr(cli_sessions, "POLL_S", 0.05)

    def server() -> None:  # the process that owns the session sees the request and closes it
        with Registry(tmp_home) as mine:
            for _ in range(200):
                row = mine.get_session(public)
                if row is not None and row.close_requested:
                    mine.close_session(public, "operator", "closed by the operator", now=time.time())
                    return
                time.sleep(0.02)

    thread = threading.Thread(target=server)
    thread.start()
    assert main(["sessions", "close", public]) == 0
    thread.join()
    assert "the server audited it" in capsys.readouterr().out


def test_a_live_session_whose_server_does_not_answer_is_reported_not_forced(
    registry: Registry,
    version: VersionRecord,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, public = open_one(registry, version)
    removed: list[object] = []
    monkeypatch.setattr(cli_sessions, "_remove_container", lambda *a: removed.append(a) or True)
    monkeypatch.setattr(cli_sessions, "WAIT_S", 0.2)
    monkeypatch.setattr(cli_sessions, "POLL_S", 0.05)
    monkeypatch.setattr(
        cli_sessions,
        "_close_one",
        lambda settings, registry, public, wait_s=0.2, _f=cli_sessions._close_one: _f(
            settings, registry, public, wait_s=wait_s
        ),
    )
    assert main(["sessions", "close", public]) == 1
    assert "has not closed it yet" in capsys.readouterr().out
    assert (
        removed == []
        and registry.get_session(public).close_requested
        and registry.get_session(public).is_open
    )


def test_close_all_closes_each_open_session(
    registry: Registry,
    version: VersionRecord,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = open_one(registry, version, now=time.time() - 600, box="tf-a")[1]
    b = open_one(registry, version, now=time.time() - 600, box="tf-b")[1]
    monkeypatch.setattr(cli_sessions, "_remove_container", lambda settings, name: True)
    assert main(["sessions", "close", "--all"]) == 0
    assert not registry.get_session(a).is_open and not registry.get_session(b).is_open
    assert main(["sessions", "close", "--all"]) == 0
    assert "no open sessions" in capsys.readouterr().out


def test_closing_a_closed_session_is_not_an_error(
    registry: Registry, version: VersionRecord, capsys: pytest.CaptureFixture[str]
) -> None:
    _, public = open_one(registry, version)
    registry.close_session(public, "closed", "by the caller", now=time.time())
    assert main(["sessions", "close", public]) == 0
    assert "already closed" in capsys.readouterr().out
