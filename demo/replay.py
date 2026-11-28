"""Scripted agent turns, and the model object that plays them back.

A transcript is the agent's side of one act: the JSON objects a model would reply with, in order. Replaying it means the **agent** is
scripted and everything else is real: the MCP server, the registry, the gates, the sandbox and the ledger all run for real, so the demo
needs no language model and has no variance. The turns in `demo/transcripts/` were written by `demo/make_transcripts.py` from the
reference packs; they were **not** produced by a language model, and every transcript says so (`"source": "scripted"`). A live run
(`--live`) can save the turns a real model produced in the same format.

Two things keep a replay honest:

* `expect` on a turn lists text that the previous tool result must contain. When the real server answers differently from what the
  script assumed (a gate failed that should have passed, say), the replay stops with `ReplayMismatch` and says what was expected; it
  never plays on as if nothing had happened.
* `requires: "isolation"` marks a turn that must only run inside a real sandbox (the second hostile attempt of the attack act). With
  the stand-in sandbox the turn is left out, and the demo says so.

The string `"$session_id"` anywhere in a turn's arguments is replaced with the session id that the most recent tool result carried
(session ids are random, so a script cannot know them in advance).

Proven by `tests/test_demo_replay.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA = 1
SESSION_PLACEHOLDER = "$session_id"
SESSION_ID = re.compile(r'"session_id"\s*:\s*"(s-[0-9a-f]{8}\.[0-9a-f]{32})"')
MAX_TURNS = 40


class ReplayError(Exception):
    """A transcript that cannot be played (unreadable, wrong shape, or it ran out of turns)."""


class ReplayMismatch(ReplayError):
    """The real system answered differently from what the script expected."""


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: dict[str, Any]
    say: str | None = Field(default=None, max_length=300)  # one line the demo may show before the call
    expect: list[str] = Field(default_factory=list, max_length=8)
    requires: str | None = None  # "isolation" or None

    def model_post_init(self, __context: Any) -> None:
        action = self.reply.get("action")
        if action not in ("call", "final"):
            raise ValueError("a turn's reply must have action 'call' or 'final'")
        if action == "call" and not isinstance(self.reply.get("tool"), str):
            raise ValueError("a call needs a tool name")
        if action == "final" and "answer" not in self.reply:
            raise ValueError("a final reply needs an answer")
        if self.requires not in (None, "isolation"):
            raise ValueError("requires must be null or 'isolation'")


class TaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    seed: int
    sibling: bool = False


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(alias="schema")
    act: str
    source: str  # "scripted" or "live"
    note: str = ""
    task: TaskRef
    turns: list[Turn] = Field(min_length=1, max_length=MAX_TURNS)

    def model_post_init(self, __context: Any) -> None:
        if self.schema_version != SCHEMA:
            raise ValueError(f"transcript schema {self.schema_version} is not supported (expected {SCHEMA})")
        if self.source not in ("scripted", "live"):
            raise ValueError("source must be 'scripted' or 'live'")
        if self.turns[-1].reply.get("action") != "final":
            raise ValueError("the last turn must be a final answer")

    def tools_called(self) -> list[str]:
        return [str(t.reply["tool"]) for t in self.turns if t.reply.get("action") == "call"]

    def to_json(self) -> str:
        data = self.model_dump(by_alias=True, exclude_none=True)
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_transcript(path: Path) -> Transcript:
    try:
        return Transcript.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayError(f"{path.name} is not a usable transcript: {str(exc)[:300]}") from exc


def load_transcripts(directory: Path) -> dict[str, Transcript]:
    """Every `*.json` in `directory`, by act id. Two files for one act, or no file at all, is an error."""
    if not directory.is_dir():
        raise ReplayError(f"{directory} is not a directory of transcripts")
    found: dict[str, Transcript] = {}
    for path in sorted(directory.glob("*.json")):
        transcript = load_transcript(path)
        if transcript.act in found:
            raise ReplayError(f"two transcripts are for the act {transcript.act!r}")
        found[transcript.act] = transcript
    if not found:
        raise ReplayError(f"{directory} holds no transcript (*.json)")
    return found


@dataclass(frozen=True)
class Reply:
    """What `bench.agent.Agent` reads from a model: the text, the model name and (never measured here) token counts."""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def substitute(value: Any, session_id: str | None) -> Any:
    if value == SESSION_PLACEHOLDER:
        if session_id is None:
            raise ReplayMismatch("a turn needs a session id, but no tool result has carried one yet")
        return session_id
    if isinstance(value, dict):
        return {k: substitute(v, session_id) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, session_id) for v in value]
    return value


def last_session_id(messages: list[dict[str, str]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "user":
            found = SESSION_ID.findall(message.get("content", ""))
            if found:
                return found[-1]
    return None


def last_tool_text(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


class ReplayModel:
    """A `ChatModel` that answers with the transcript's turns, one per request. `isolated` says whether turns that need a real sandbox play."""

    model = "scripted-replay"

    def __init__(self, transcript: Transcript, *, isolated: bool) -> None:
        self.transcript = transcript
        self.turns = [t for t in transcript.turns if t.requires is None or isolated]
        self.skipped = len(transcript.turns) - len(self.turns)
        self.position = 0
        self.said: list[str] = []  # the `say` lines in the order they were reached

    def chat(self, messages: list[dict[str, str]], *, deadline: float | None = None) -> Reply:
        if self.position >= len(self.turns):
            raise ReplayError(
                f"the {self.transcript.act!r} transcript ended after {len(self.turns)} turns, but the agent loop asked for another"
            )
        turn = self.turns[self.position]
        seen = last_tool_text(messages)
        for needle in turn.expect:
            if needle not in seen:
                raise ReplayMismatch(
                    f"turn {self.position + 1} of the {self.transcript.act!r} transcript expected the previous result to contain "
                    f"{needle!r}; the real system answered differently, so the script stops here"
                )
        self.position += 1
        if turn.say:
            self.said.append(turn.say)
        reply = substitute(turn.reply, last_session_id(messages))
        return Reply(text=json.dumps(reply, ensure_ascii=False), model=self.model)

    @property
    def finished(self) -> bool:
        return self.position >= len(self.turns)


class TurnRecorder:
    """Wraps a live model and keeps the JSON objects it answered with, to save as a transcript (`source: live`)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.model = getattr(inner, "model", "") or ""
        self.replies: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, str]], *, deadline: float | None = None) -> Any:
        reply = self.inner.chat(messages, deadline=deadline)
        try:
            data = json.loads(getattr(reply, "text", "") or "")
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("action") in ("call", "final"):
            self.replies.append(data)
        return reply

    def transcript(self, act: str, task: TaskRef) -> Transcript | None:
        if not self.replies or self.replies[-1].get("action") != "final":
            return None
        return Transcript(
            schema=SCHEMA,
            act=act,
            source="live",
            note=f"saved from a live run with model {self.model or 'unknown'}",
            task=task,
            turns=[Turn(reply=r) for r in self.replies[:MAX_TURNS]],
        )
