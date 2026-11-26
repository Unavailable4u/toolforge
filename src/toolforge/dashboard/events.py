"""The dashboard's live feed (P17): new ledger events and finished calls, as server-sent events.

`EventFeed` is plain code with no web framework in it. `cursor()` marks "now" (so a new viewer sees only what happens next, or the
newest `replay` events when asked) and `poll(cursor)` returns what is new since. `app.py` turns that into a stream.

Two sources, both read-only:

* the registry's `calls` table, for call completions (`kind: "call"`), through `Registry.calls_after`;
* the ledger files under `$TOOLFORGE_HOME/ledgers/*/*.jsonl`, for lifecycle, gate, approval and operator events (`kind: "ledger"`). Each
  file is read from the byte where the last poll stopped, up to its last complete line, so a half-written line is read next time and never
  dropped. A file that shrank (replaced) is read from its start again.

An event leaves here as a small summary: its type, actor, time, tool label and a few scalar fields cut to short text. Full payloads, call
arguments and call results never go on the stream. Sessions are shown by their public id only.

Bounds: at most `MAX_FILES` ledger files are looked at per poll (the newest by modification time), at most `MAX_READ_BYTES` read per file per
poll, and at most `MAX_EVENTS_PER_POLL` events returned. Past a bound the call completions still arrive from the registry; only some
ledger events are late, and they are read on a later poll.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from toolforge.config import Settings
from toolforge.registry import Registry
from toolforge.textsafety import sanitize

MAX_FILES = 5000
MAX_READ_BYTES = 256 * 1024
MAX_EVENTS_PER_POLL = 200
MAX_FIELDS = 12
FIELD_CHARS = 160
# Payload keys that are safe to show as they are (short facts, never tool-authored result text).
_KEYS = (
    "tool",
    "version",
    "from",
    "to",
    "reason",
    "gate",
    "status",
    "ok",
    "approval_id",
    "outcome",
    "decision",
    "duration_ms",
    "error_class",
    "timed_out",
    "session",
    "steps",
)


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return sanitize(value, max_chars=FIELD_CHARS)
    return None


def summarize_event(event: dict[str, Any], label: str) -> dict[str, Any]:
    """One ledger event as the stream shows it."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    fields = {k: _scalar(payload[k]) for k in _KEYS if k in payload and _scalar(payload[k]) is not None}
    if "session" in fields and isinstance(fields["session"], str):
        fields["session"] = fields["session"].partition(".")[0]
    ts = event.get("ts")
    return {
        "kind": "ledger",
        "type": sanitize(event.get("type", ""), max_chars=80),
        "actor": sanitize(event.get("actor", ""), max_chars=80),
        "label": sanitize(label, max_chars=120),
        "ts": float(ts) if isinstance(ts, int | float) else None,
        "fields": dict(list(fields.items())[:MAX_FIELDS]),
    }


class EventFeed:
    def __init__(self, settings: Settings, registry: Registry) -> None:
        self._settings = settings
        self._registry = registry

    @property
    def _root(self) -> Path:
        return Path(self._settings.home) / "ledgers"

    def _files(self) -> list[tuple[Path, int, float]]:
        """`(path, size, mtime)` of the ledger files, newest first, at most MAX_FILES."""
        out: list[tuple[Path, int, float]] = []
        try:
            directories = [e for e in os.scandir(self._root) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return []
        for directory in directories:
            try:
                for entry in os.scandir(directory.path):
                    if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        out.append((Path(entry.path), st.st_size, st.st_mtime))
            except OSError:
                continue
        out.sort(key=lambda item: item[2], reverse=True)
        return out[:MAX_FILES]

    def cursor(self, replay: int = 0) -> dict[str, Any]:
        """Where "now" is. With `replay`, the newest `replay` events that already exist are delivered by the first poll."""
        replay = max(0, min(int(replay), 100))
        files = self._files()
        offsets = {str(p): size for p, size, _ in files}
        call_id = self._registry.last_call_id()
        if replay:
            for p, _, _ in files[: min(replay, 20)]:
                offsets[str(p)] = 0
            call_id = max(0, call_id - replay)
        return {"call_id": call_id, "offsets": offsets, "replay": replay, "seq": 0}

    def _read_new(self, path: Path, start: int, size: int) -> tuple[list[dict[str, Any]], int]:
        if size < start:  # replaced: read it again from the top
            start = 0
        if size == start:
            return [], start
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                chunk = handle.read(min(size - start, MAX_READ_BYTES))
        except OSError:
            return [], start
        end = chunk.rfind(b"\n")
        if end < 0:  # no complete line yet (or one line longer than the read cap: skip it rather than stall)
            return [], start + len(chunk) if len(chunk) >= MAX_READ_BYTES else start
        events: list[dict[str, Any]] = []
        for line in chunk[: end + 1].splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events, start + end + 1

    def poll(self, cursor: dict[str, Any]) -> list[dict[str, Any]]:
        """Everything new since `cursor`, oldest first; the cursor is advanced in place."""
        found: list[dict[str, Any]] = []
        offsets: dict[str, int] = cursor["offsets"]
        for path, size, _ in self._files():
            key = str(path)
            if key not in offsets:
                offsets[key] = 0  # a file created since the last poll: everything in it is new
            events, new_offset = self._read_new(path, offsets[key], size)
            offsets[key] = new_offset
            label = path.parent.name
            found += [summarize_event(e, label) for e in events]
        for row in self._registry.calls_after(cursor["call_id"], limit=MAX_EVENTS_PER_POLL):
            cursor["call_id"] = max(cursor["call_id"], row.id)
            found.append(
                {
                    "kind": "call",
                    "type": "call.finished",
                    "actor": None,
                    "label": row.label,
                    "ts": None,
                    "when": row.ts,
                    "fields": {
                        "ok": row.ok,
                        "duration_ms": row.duration_ms,
                        **(
                            {"error_class": sanitize(row.error_class, max_chars=60)}
                            if row.error_class
                            else {}
                        ),
                    },
                }
            )
        found.sort(key=lambda e: (e["ts"] is None, e["ts"] or 0.0))
        replay = cursor.get("replay", 0)
        limit = replay if replay else MAX_EVENTS_PER_POLL
        cursor["replay"] = 0  # only the first poll replays
        found = found[-limit:]
        for event in found:
            cursor["seq"] += 1
            event["id"] = cursor["seq"]
        return found
