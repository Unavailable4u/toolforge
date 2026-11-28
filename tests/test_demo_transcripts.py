"""The scripted transcripts are what the generator writes, parse, call tools that exist, and give answers the hidden checker accepts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo import acts, hostile, make_transcripts
from demo.replay import load_transcripts
from toolforge.server.tools import META_TOOLS

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / "demo" / "transcripts"


def test_the_committed_transcripts_are_what_the_generator_writes() -> None:
    assert make_transcripts.differences(DIRECTORY) == [], "run python demo/make_transcripts.py"


def test_generating_twice_gives_the_same_text() -> None:
    first = {n: t.to_json() for n, t in make_transcripts.build_all().items()}
    second = {n: t.to_json() for n, t in make_transcripts.build_all().items()}
    assert first == second


def test_there_is_one_scripted_transcript_per_act() -> None:
    found = load_transcripts(DIRECTORY)
    assert sorted(found) == sorted(a.id for a in acts.ACTS)
    for act in acts.ACTS:
        transcript = found[act.id]
        assert transcript.source == "scripted" and "not produced by a language model" in transcript.note
        assert (
            transcript.task.id == act.task
            and transcript.task.seed == acts.SEED
            and transcript.task.sibling == act.sibling
        )


def test_every_call_names_a_tool_the_server_offers() -> None:
    offered = {tool.name for tool in META_TOOLS}
    for transcript in load_transcripts(DIRECTORY).values():
        assert set(transcript.tools_called()) <= offered, transcript.act


def test_a_tool_that_is_run_was_submitted_earlier_in_the_demo() -> None:
    submitted: set[str] = set()
    for act in acts.ACTS:  # acts run in this order against one home
        transcript = load_transcripts(DIRECTORY)[act.id]
        for turn in transcript.turns:
            reply = turn.reply
            if reply.get("action") != "call":
                continue
            arguments = reply["arguments"]
            if reply["tool"] == "forge_submit":
                submitted.add(arguments["package"]["manifest"]["name"])
            if reply["tool"] == "forge_run":
                assert arguments["name"] in submitted, f"{act.id} runs {arguments['name']} before it exists"


def test_every_scripted_answer_passes_the_hidden_checker() -> None:
    for act in acts.ACTS:
        transcript = load_transcripts(DIRECTORY)[act.id]
        task = acts.task_for(act)
        instance = task.generate(acts.SEED, act.sibling)
        assert task.check(instance, transcript.turns[-1].reply["answer"], {}).ok, act.id


def test_only_the_second_hostile_attempt_needs_a_real_sandbox() -> None:
    transcripts = load_transcripts(DIRECTORY)
    needing = [
        (act, turn.reply["arguments"]["package"]["manifest"]["name"])
        for act, transcript in transcripts.items()
        for turn in transcript.turns
        if turn.requires == "isolation"
    ]
    assert needing == [("attack", hostile.ATTEMPT_TWO)]


def test_hostile_source_appears_only_in_the_attack_transcript() -> None:
    for name in ("01-forge.json", "02-reuse.json", "04-software.json"):
        assert hostile.MARKER not in (DIRECTORY / name).read_text(encoding="utf-8"), name
    text = (DIRECTORY / "03-attack.json").read_text(encoding="utf-8")
    assert text.count(hostile.MARKER) == 2


def test_no_transcript_holds_a_real_looking_secret_or_a_path_of_this_machine() -> None:
    from toolforge.verify import secretscan

    for path in DIRECTORY.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert not secretscan.scan(text, path.name), path.name
        assert "/home/" not in text and "/Users/" not in text, path.name


def test_the_files_are_plain_sorted_json() -> None:
    for path in DIRECTORY.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert (
            path.read_text(encoding="utf-8")
            == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )


@pytest.mark.parametrize("spec", ["", "all", "1,2", "forge,reuse", "3", "attack,4", "1,2,3,4"])
def test_act_selection_accepts_numbers_and_names(spec: str) -> None:
    chosen = acts.select(spec)
    assert [a.number for a in chosen] == sorted(a.number for a in chosen)


@pytest.mark.parametrize("spec", ["2", "reuse", "5", "nonsense", "3,reuse"])
def test_act_selection_refuses_unknown_acts_and_an_act_without_what_it_reuses(spec: str) -> None:
    with pytest.raises(ValueError):
        acts.select(spec)
