"""ToolforgeLedger: creates its directory, fsyncs every append, keeps MiniLocker's hash chain and notices tampering."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from toolforge import ledger as ledger_module
from toolforge.errors import LedgerError, SandboxUnavailable
from toolforge.ledger import ToolforgeLedger
from toolforge.sandbox import _ml_path

pytestmark = pytest.mark.usefixtures("minilocker")


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_parent_directories_are_created(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "run.jsonl"
    ledger = ToolforgeLedger(path, "t1")
    assert path.parent.is_dir() and not path.exists()  # created for the first append, not before
    ledger.append("forge", "sandbox.created", {"name": "x"})
    assert path.is_file()


def test_append_returns_a_chained_event_and_verify_passes(tmp_path: Path) -> None:
    ledger = ToolforgeLedger(tmp_path / "l.jsonl", "t1")
    first = ledger.append("a", "one", {"k": 1})
    second = ledger.append("a", "two")
    assert (
        (first["id"], second["id"]) == (0, 1)
        and second["prev_hash"] == first["hash"]
        and second["payload"] == {}
    )
    assert ledger.verify()[0] is True
    assert [e["hash"] for e in lines(tmp_path / "l.jsonl")] == [first["hash"], second["hash"]]


def test_every_append_is_forced_to_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    synced: list[int] = []
    monkeypatch.setattr(ledger_module.os, "fsync", lambda fd: synced.append(fd))
    ledger = ToolforgeLedger(tmp_path / "l.jsonl", "t1")
    for i in range(3):
        ledger.append("a", "e", {"i": i})
        assert len(synced) == i + 1
    assert len(lines(tmp_path / "l.jsonl")) == 3  # the fsync does not add or drop lines


def test_a_memory_only_ledger_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    synced: list[int] = []
    monkeypatch.setattr(ledger_module.os, "fsync", lambda fd: synced.append(fd))
    ledger = ToolforgeLedger(None, "t1")
    ledger.append("a", "e")
    assert ledger.path is None and ledger.verify()[0] is True and len(ledger.events) == 1 and synced == []
    assert list(tmp_path.iterdir()) == []


def test_an_existing_file_is_resumed_not_restarted(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    first = ToolforgeLedger(path, "t1")
    first.append("a", "one")
    tail = first.append("a", "two")
    again = ToolforgeLedger(path, "t1")
    event = again.append("a", "three")
    assert event["id"] == 2 and event["prev_hash"] == tail["hash"] and again.verify()[0] is True


def test_tampering_with_a_written_event_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ledger = ToolforgeLedger(path, "t1")
    ledger.append("a", "sandbox.created", {"hardening": {"network_mode": "none"}})
    ledger.append("a", "sandbox.destroyed")
    path.write_text(path.read_text().replace('"network_mode":"none"', '"network_mode":"host"'))
    ok, message = ToolforgeLedger(path, "t1").verify()
    assert ok is False and message  # the file changed under the chain


def test_a_damaged_file_is_a_ledger_error_with_a_hint(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    path.write_text("{not json\n")
    with pytest.raises(LedgerError) as caught:
        ToolforgeLedger(path, "t1")
    assert caught.value.code == "ledger_error" and "do not edit" in (caught.value.hint or "")


def test_events_is_a_copy_of_the_list(tmp_path: Path) -> None:
    ledger = ToolforgeLedger(tmp_path / "l.jsonl", "t1")
    ledger.append("a", "e")
    ledger.events.clear()
    assert len(ledger.events) == 1


def test_appends_from_several_threads_keep_one_intact_chain(tmp_path: Path) -> None:
    ledger = ToolforgeLedger(tmp_path / "l.jsonl", "t1")

    def work(n: int) -> None:
        for i in range(25):
            ledger.append(f"t{n}", "e", {"i": i})

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = lines(tmp_path / "l.jsonl")
    assert len(events) == 200 and [e["id"] for e in events] == list(range(200)) and ledger.verify()[0] is True


def test_without_minilocker_construction_says_how_to_get_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ml_path.reset_for_tests()
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(tmp_path))
    with pytest.raises(SandboxUnavailable):
        ToolforgeLedger(None, "t1")
