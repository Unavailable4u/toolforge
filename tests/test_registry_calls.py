"""`Registry.recent_calls`, `calls_after` and `last_call_id`: read-only views of the calls table that the dashboard polls."""

from __future__ import annotations

from pathlib import Path

import pytest
from dashboard_helpers import make_settings, submit

from toolforge.registry import Registry


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def test_calls_are_listed_newest_first_and_page_backwards(tmp_path, registry) -> None:
    submit(make_settings(tmp_path), registry)
    record = registry.find_version("my_tool", "0.1.0")
    for n in range(5):
        registry.record_call(record.id, n % 2 == 0, n * 10, None if n % 2 == 0 else "ToolError")
    first = registry.recent_calls(record.id, limit=2)
    assert (
        [c.duration_ms for c in first] == [40, 30]
        and first[0].label == "my_tool@0.1.0"
        and first[0].ok is True
    )
    second = registry.recent_calls(record.id, limit=2, before_id=first[-1].id)
    assert [c.duration_ms for c in second] == [20, 10]
    assert registry.recent_calls(record.id, limit=1000)  # a limit is capped, not refused
    assert registry.recent_calls(record.id + 999) == []


def test_calls_after_returns_oldest_first_for_every_version(tmp_path, registry) -> None:
    settings = make_settings(tmp_path)
    submit(settings, registry)
    submit(settings, registry, name="other_tool")
    one, two = registry.find_version("my_tool", "0.1.0"), registry.find_version("other_tool", "0.1.0")
    assert registry.last_call_id() == 0 and registry.calls_after(0) == []
    registry.record_call(one.id, True, 1)
    registry.record_call(two.id, False, 2, "Crash", session_id="s-1.x")
    rows = registry.calls_after(0)
    assert [(c.label, c.ok, c.error_class) for c in rows] == [
        ("my_tool@0.1.0", True, None),
        ("other_tool@0.1.0", False, "Crash"),
    ]
    assert registry.last_call_id() == rows[-1].id
    assert (
        registry.calls_after(rows[0].id)[0].label == "other_tool@0.1.0"
        and registry.calls_after(rows[-1].id) == []
    )
    assert len(registry.calls_after(0, limit=1)) == 1
