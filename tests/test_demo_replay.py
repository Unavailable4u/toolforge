"""The replay model: scripted turns played one per request, checked against what the real system said, never played on after a mismatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo.replay import (
    SCHEMA,
    ReplayError,
    ReplayMismatch,
    ReplayModel,
    TaskRef,
    Transcript,
    Turn,
    TurnRecorder,
    last_session_id,
    load_transcript,
    load_transcripts,
    substitute,
)

SESSION = "s-0123abcd.0123456789abcdef0123456789abcdef"


def transcript(*turns: Turn, act: str = "forge") -> Transcript:
    return Transcript(
        schema=SCHEMA, act=act, source="scripted", task=TaskRef(id="sqlite_join", seed=7), turns=list(turns)
    )


def call(tool: str = "forge_find", **extra: object) -> Turn:
    return Turn(reply={"action": "call", "tool": tool, "arguments": {}}, **extra)  # type: ignore[arg-type]


def final(answer: object = {"x": 1}) -> Turn:
    return Turn(reply={"action": "final", "answer": answer})


def ask(model: ReplayModel, tool_text: str = "") -> dict[str, object]:
    reply = model.chat([{"role": "system", "content": "s"}, {"role": "user", "content": tool_text}])
    return json.loads(reply.text)


def test_turns_are_played_in_order_one_per_request() -> None:
    model = ReplayModel(transcript(call("forge_find"), call("forge_run"), final()), isolated=True)
    assert ask(model)["tool"] == "forge_find"
    assert ask(model)["tool"] == "forge_run"
    assert ask(model)["action"] == "final" and model.finished


def test_a_reply_carries_no_token_counts_and_names_the_replay() -> None:
    model = ReplayModel(transcript(final()), isolated=True)
    reply = model.chat([{"role": "user", "content": ""}])
    assert (
        reply.prompt_tokens is None and reply.completion_tokens is None and reply.model == "scripted-replay"
    )


def test_asking_past_the_end_is_an_error_that_says_so() -> None:
    model = ReplayModel(transcript(final()), isolated=True)
    ask(model)
    with pytest.raises(ReplayError, match="ended after 1 turns"):
        ask(model)


def test_an_expectation_that_the_real_system_did_not_meet_stops_the_script() -> None:
    model = ReplayModel(transcript(call(), call("forge_run", expect=['"count": 0']), final()), isolated=True)
    ask(model)
    with pytest.raises(ReplayMismatch, match="answered differently"):
        ask(model, '{"count": 3}')


def test_an_expectation_that_holds_lets_the_script_go_on() -> None:
    model = ReplayModel(transcript(call(), call("forge_run", expect=["verified"]), final()), isolated=True)
    ask(model)
    assert ask(model, '{"status": "verified_basic"}')["tool"] == "forge_run"


def test_turns_that_need_isolation_are_left_out_without_it() -> None:
    turns = [call("a"), call("b", requires="isolation"), final()]
    plain = ReplayModel(transcript(*turns), isolated=False)
    assert plain.skipped == 1 and [ask(plain)["tool"], ask(plain)["action"]] == ["a", "final"]
    full = ReplayModel(transcript(*turns), isolated=True)
    assert full.skipped == 0 and [ask(full)["tool"], ask(full)["tool"]] == ["a", "b"]


def test_the_session_placeholder_takes_the_id_the_last_result_carried() -> None:
    turn = Turn(
        reply={
            "action": "call",
            "tool": "forge_run",
            "arguments": {"session_id": "$session_id", "n": [1, "$session_id"]},
        }
    )
    model = ReplayModel(transcript(turn, final()), isolated=True)
    sent = ask(model, f'{{"ok": true, "session_id": "{SESSION}", "step": 1}}')
    assert sent["arguments"] == {"session_id": SESSION, "n": [1, SESSION]}


def test_the_session_placeholder_without_a_session_is_a_mismatch() -> None:
    with pytest.raises(ReplayMismatch, match="session id"):
        substitute({"a": "$session_id"}, None)


def test_the_newest_session_id_wins() -> None:
    other = "s-ffffffff.ffffffffffffffffffffffffffffffff"
    messages = [
        {"role": "user", "content": f'"session_id": "{SESSION}"'},
        {"role": "user", "content": f'"session_id": "{other}"'},
    ]
    assert last_session_id(messages) == other


@pytest.mark.parametrize(
    "reply",
    [{"action": "think"}, {"action": "call"}, {"action": "final"}, {"action": "call", "tool": 3}],
)
def test_a_turn_must_be_a_call_with_a_tool_or_a_final_answer(reply: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Turn(reply=reply)


def test_the_last_turn_must_be_a_final_answer_and_the_schema_must_match() -> None:
    with pytest.raises(ValueError):
        transcript(call())
    with pytest.raises(ValueError):
        Transcript(schema=99, act="x", source="scripted", task=TaskRef(id="t", seed=1), turns=[final()])
    with pytest.raises(ValueError):
        Transcript(schema=SCHEMA, act="x", source="invented", task=TaskRef(id="t", seed=1), turns=[final()])


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValueError):
        Turn(reply={"action": "final", "answer": 1}, surprise=True)  # type: ignore[call-arg]


def test_a_transcript_round_trips_through_its_json(tmp_path: Path) -> None:
    original = transcript(call(say="look"), final())
    path = tmp_path / "t.json"
    path.write_text(original.to_json(), encoding="utf-8")
    assert load_transcript(path) == original
    assert original.tools_called() == ["forge_find"]


def test_loading_names_the_file_when_it_is_not_a_transcript(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ReplayError, match="bad.json"):
        load_transcripts(tmp_path)


def test_loading_refuses_two_files_for_one_act_and_an_empty_folder(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="no transcript"):
        load_transcripts(tmp_path)
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(transcript(final()).to_json(), encoding="utf-8")
    with pytest.raises(ReplayError, match="two transcripts"):
        load_transcripts(tmp_path)
    with pytest.raises(ReplayError, match="not a directory"):
        load_transcripts(tmp_path / "missing")


def test_the_recorder_keeps_what_a_live_model_said_and_only_a_finished_run_is_a_transcript() -> None:
    class Live:
        model = "m"

        def __init__(self) -> None:
            self.answers = iter(
                [
                    '{"action": "call", "tool": "forge_find", "arguments": {}}',
                    "not json",
                    '{"action": "final", "answer": 3}',
                ]
            )

        def chat(self, messages: list[dict[str, str]], *, deadline: float | None = None) -> object:
            return type(
                "R",
                (),
                {"text": next(self.answers), "model": "m", "prompt_tokens": 1, "completion_tokens": 1},
            )()

    recorder = TurnRecorder(Live())
    task = TaskRef(id="sqlite_join", seed=7)
    recorder.chat([])
    assert recorder.transcript("forge", task) is None
    recorder.chat([])
    recorder.chat([])
    saved = recorder.transcript("forge", task)
    assert saved is not None and saved.source == "live" and len(saved.turns) == 2
