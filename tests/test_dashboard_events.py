"""The live feed: new ledger events and finished calls, summarised, bounded, and never carrying a secret."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dashboard_helpers import TOKEN, auth, make_dashboard, make_settings, submit

from toolforge.dashboard.events import EventFeed, summarize_event
from toolforge.ledger import ToolforgeLedger
from toolforge.registry import Registry


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def ledger(settings, label: str, name: str = "operator.jsonl") -> ToolforgeLedger:
    return ToolforgeLedger(str(Path(settings.home) / "ledgers" / label / name), f"test:{label}")


def test_a_new_viewer_sees_only_what_happens_after_it_connects(tmp_path, registry) -> None:
    settings = make_settings(tmp_path)
    submit(settings, registry)
    record = registry.find_version("my_tool", "0.1.0")
    registry.record_call(record.id, True, 5)
    ledger(settings, "my_tool@0.1.0").append("operator", "forge.promoted", {"reason": "before"})
    feed = EventFeed(settings, registry)
    cursor = feed.cursor()
    assert feed.poll(cursor) == []
    registry.record_call(record.id, False, 40, "ToolError")
    ledger(settings, "my_tool@0.1.0").append(
        "operator", "forge.quarantined", {"reason": "after", "from": "active", "to": "quarantined"}
    )
    events = feed.poll(cursor)
    assert {e["kind"] for e in events} == {"call", "ledger"}
    call = next(e for e in events if e["kind"] == "call")
    assert call["label"] == "my_tool@0.1.0" and call["fields"] == {
        "ok": False,
        "duration_ms": 40,
        "error_class": "ToolError",
    }
    led = next(e for e in events if e["kind"] == "ledger")
    assert (
        led["type"] == "forge.quarantined"
        and led["actor"] == "operator"
        and led["fields"]["reason"] == "after"
    )
    assert [e["id"] for e in events] == [1, 2] and feed.poll(cursor) == []


def test_replay_delivers_the_newest_existing_events_once(tmp_path, registry) -> None:
    settings = make_settings(tmp_path)
    submit(settings, registry)
    record = registry.find_version("my_tool", "0.1.0")
    for n in range(5):
        registry.record_call(record.id, True, n)
    feed = EventFeed(settings, registry)
    cursor = feed.cursor(replay=3)
    first = feed.poll(cursor)
    assert [e["fields"]["duration_ms"] for e in first if e["kind"] == "call"][-3:] == [2, 3, 4]
    assert len(first) <= 3 and feed.poll(cursor) == []


def test_a_half_written_line_is_read_next_time_not_dropped(tmp_path, registry) -> None:
    settings = make_settings(tmp_path)
    feed = EventFeed(settings, registry)
    cursor = feed.cursor()
    path = Path(settings.home) / "ledgers" / "t@1" / "operator.jsonl"
    path.parent.mkdir(parents=True)
    line = (
        json.dumps({"type": "forge.promoted", "actor": "operator", "ts": 1.0, "payload": {"reason": "ok"}})
        + "\n"
    )
    path.write_text(line[:30])
    assert feed.poll(cursor) == []
    path.write_text(line + line[:10])
    assert [e["type"] for e in feed.poll(cursor)] == ["forge.promoted"]
    path.write_text(line + line)
    assert [e["type"] for e in feed.poll(cursor)] == ["forge.promoted"]


def test_an_event_leaves_as_a_small_summary_without_the_payload(tmp_path) -> None:
    event = {
        "type": "forge.promoted\u200b",
        "actor": "operator",
        "ts": 12.5,
        "payload": {
            "tool": "t",
            "reason": "x" * 1000,
            "session": "s-abcd1234.SECRETHALF",
            "secret_value": "hunter2",
            "result": {"rows": [1, 2, 3]},
            "approval_id": 4,
            "ok": True,
        },
    }
    shown = summarize_event(event, "t@1")
    assert shown["type"] == "forge.promoted" and shown["label"] == "t@1" and shown["ts"] == 12.5
    assert set(shown["fields"]) == {"tool", "reason", "session", "approval_id", "ok"}
    assert len(shown["fields"]["reason"]) <= 200 and shown["fields"]["session"] == "s-abcd1234"
    assert "hunter2" not in json.dumps(shown) and "SECRETHALF" not in json.dumps(shown)


def test_the_stream_sends_ready_then_events_and_stops_at_max_events(tmp_path, registry) -> None:
    settings = make_settings(tmp_path)
    submit(settings, registry)
    record = registry.find_version("my_tool", "0.1.0")
    registry.record_call(record.id, True, 7, session_id="s-1234abcd.SECRETHALF")
    _, client, _ = make_dashboard(tmp_path, registry)
    with client.stream("GET", "/api/events?replay=5&max_events=1") as res:
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
        assert res.headers["cache-control"] == "no-cache"
        body = "".join(res.iter_text())
    frames = [f for f in body.split("\n\n") if f]
    assert frames[0].startswith("event: ready")
    assert frames[1].startswith("id: 1\nevent: call\ndata: ")
    data = json.loads(frames[1].split("data: ", 1)[1])
    assert data["label"] == "my_tool@0.1.0" and data["fields"]["duration_ms"] == 7
    assert "SECRETHALF" not in body and len(frames) == 2


def test_the_stream_needs_the_token(tmp_path, registry) -> None:
    _, client, _ = make_dashboard(tmp_path, registry, dashboard_token=TOKEN)
    assert client.get("/api/events?max_events=1").status_code == 401
    with client.stream("GET", "/api/events?max_events=1&replay=0", headers=auth()) as res:
        assert res.status_code == 200
        first = next(res.iter_text())
    assert first.startswith("event: ready")
    assert client.get("/api/events?max_events=0", headers=auth()).status_code == 422
