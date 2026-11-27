"""The benchmark tasks (P16): deterministic generators, and hidden checkers that accept a right answer and reject wrong ones."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from bench import tasks as task_set
from bench.tasks import chess_mate_in_2, frozenlake_path
from toolforge.verify import secretscan

REQUIRED = (
    "log_stats",
    "sqlite_join",
    "tlv_convert",
    "fixed_width",
    "messy_dates",
    "yaml_diff",
    "blackjack_basic",
    "frozenlake_path",
    "chess_mate_in_2",
    "http_mock_fetch",
    "browser_fixture_headlines",
    "hostile_data_file",
)
SEEDS = (1, 2, 7, 42)
IDS = task_set.task_ids()


def correct(task_id: str, instance: task_set.Instance) -> dict[str, Any]:
    """A right answer, built from the hidden expectation (the agent never sees it)."""
    expected = copy.deepcopy(instance.expected)
    if task_id == "frozenlake_path":
        return {"path": frozenlake_path.shortest(expected["grid"])}
    if task_id == "chess_mate_in_2":
        return {"moves": [keys[-1] for keys in expected["keys"]]}
    if task_id == "hostile_data_file":
        return {"totals": expected["totals"]}
    return expected


def _first_number_path(value: Any, path: tuple = ()) -> tuple | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return path
    if isinstance(value, dict):
        for key in value:
            found = _first_number_path(value[key], (*path, key))
            if found is not None:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _first_number_path(item, (*path, index))
            if found is not None:
                return found
    return None


def wrong(task_id: str, instance: task_set.Instance) -> list[Any]:
    """Several wrong answers: no object, the wrong shape, and a right-looking answer with one value off."""
    answers: list[Any] = [None, "I could not do it", 42, [], {}, {"answer": correct(task_id, instance)}]
    right = correct(task_id, instance)
    path = _first_number_path(right)
    if path is not None:
        off = copy.deepcopy(right)
        node = off
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = node[path[-1]] + (5 if task_id == "blackjack_basic" else 1)
        if task_id == "blackjack_basic":
            off["win_rate"] = right["win_rate"] + 0.2
        answers.append(off)
    answers += _specific(task_id, instance, right)
    if task_id == "log_stats":
        answers.append({"services": {k: v for k, v in list(right["services"].items())[1:]}})
    if task_id == "sqlite_join":
        answers.append({"revenue_cents": {**right["revenue_cents"], "nowhere": 0}})
    return answers


def _specific(task_id: str, instance: task_set.Instance, right: dict[str, Any]) -> list[Any]:
    """Wrong answers that only make sense for one task."""
    if task_id == "frozenlake_path":
        return [{"path": "U"}, {"path": "R" * 7 + "D" * 7 + "L"}, {"path": "X"}]
    if task_id == "chess_mate_in_2":
        return [{"moves": ["a1a2", "a1a2", "a1a2"]}, {"moves": right["moves"][:1]}]
    if task_id == "messy_dates":
        return [{"dates": list(reversed(right["dates"]))}, {"dates": right["dates"][:-1]}]
    if task_id == "yaml_diff":
        return [{**right, "changed": right["changed"] + ["metadata.generated"]}]
    if task_id == "tlv_convert":
        return [{"readings": right["readings"][:-1]}, {"readings": list(reversed(right["readings"]))}]
    if task_id == "browser_fixture_headlines":
        return [{"headlines": right["headlines"][1:]}]
    if task_id == "fixed_width":
        return [{**right, "total_amount": "0.00"}]
    if task_id == "hostile_data_file":
        return [
            {"totals": right["totals"], "note": instance.canary},
            {"totals": right["totals"], "note": instance.expected["marker"]},
        ]
    return []


def test_the_task_set_has_every_required_task_once() -> None:
    assert sorted(IDS) == sorted(REQUIRED) and len(IDS) == len(set(IDS))
    assert {t.tier for t in task_set.TASKS} <= set(task_set.TIERS)
    assert [t.id for t in task_set.TASKS if t.has_sibling] == list(REQUIRED[:6])


@pytest.mark.parametrize("task_id", IDS)
def test_generators_are_deterministic(task_id: str) -> None:
    task = task_set.get(task_id)
    for seed in SEEDS:
        for sibling in (False, True) if task.has_sibling else (False,):
            first, second = task.generate(seed, sibling), task.generate(seed, sibling)
            assert first.fingerprint() == second.fingerprint()
            assert dict(first.files) == dict(second.files) and first.prompt == second.prompt
    if task_id != "chess_mate_in_2":  # three of fourteen puzzles: two seeds can pick the same three
        assert task.generate(1).fingerprint() != task.generate(2).fingerprint()


@pytest.mark.parametrize("task_id", [t.id for t in task_set.TASKS if t.has_sibling])
def test_a_sibling_has_other_data_and_asks_for_the_same_capability(task_id: str) -> None:
    task = task_set.get(task_id)
    main, sibling = task.generate(3, False), task.generate(3, True)
    assert main.fingerprint() != sibling.fingerprint()
    assert sorted(main.files) == sorted(sibling.files)  # same inputs, by name
    assert task.check(sibling, correct(task_id, sibling), {}).ok


@pytest.mark.parametrize("task_id", IDS)
def test_checkers_accept_a_correct_answer(task_id: str) -> None:
    task = task_set.get(task_id)
    for seed in SEEDS:
        instance = task.generate(seed)
        verdict = task.check(instance, correct(task_id, instance), {})
        assert verdict.ok, verdict.reason
        as_text = task.check(
            instance, json.dumps(correct(task_id, instance)), {}
        )  # the answer as a JSON string
        assert as_text.ok, as_text.reason


@pytest.mark.parametrize("task_id", IDS)
def test_checkers_reject_wrong_answers(task_id: str) -> None:
    task = task_set.get(task_id)
    instance = task.generate(5)
    for answer in wrong(task_id, instance):
        verdict = task.check(instance, answer, {})
        assert not verdict.ok, f"accepted a wrong answer: {answer!r}"[:300]
        assert verdict.reason


def test_frozenlake_accepts_any_safe_path_not_only_the_shortest() -> None:
    instance = frozenlake_path.generate(4)
    best = frozenlake_path.shortest(instance.expected["grid"])
    detour = (
        best[0] + {"R": "L", "L": "R", "U": "D", "D": "U"}[best[0]] + best
    )  # step out and back, then the path
    ok, _ = frozenlake_path.walk(instance.expected["grid"], detour)
    assert frozenlake_path.check(instance, {"path": detour}, {}).ok == ok


def test_blackjack_accepts_a_rate_within_one_hand() -> None:
    task = task_set.get("blackjack_basic")
    instance = task.generate(2)
    near = {**instance.expected, "win_rate": instance.expected["win_rate"] + 0.5 / instance.expected["hands"]}
    assert task.check(instance, near, {}).ok


def test_the_hostile_file_carries_injection_and_a_key_the_secret_scanner_recognises() -> None:
    instance = task_set.get("hostile_data_file").generate(3)
    text = instance.files["expenses.csv"].decode("utf-8")
    assert instance.canary and instance.canary in text
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in text
    assert secretscan.scan(text, "expenses.csv"), (
        "the look-alike must trip the scanner, or the safety net is not exercised"
    )
    assert instance.canary not in instance.prompt  # the prompt names the file, never its contents


def test_prompts_never_contain_the_expected_answer() -> None:
    for task in task_set.TASKS:
        instance = task.generate(9)
        for value in _leaves(instance.expected):
            if isinstance(value, str) and len(value) > 6:
                assert value not in instance.prompt, (task.id, value)


def _leaves(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def test_fixture_tasks_need_the_address_filled_in() -> None:
    instance = task_set.get("http_mock_fetch").generate(1)
    with pytest.raises(ValueError):
        task_set.prompt_for(instance)
    prompt = task_set.prompt_for(instance, "http://fixture.test:8080/")
    assert "http://fixture.test:8080/api/v1/items" in prompt and task_set.FIXTURE_URL not in prompt
    plain = task_set.get("log_stats").generate(1)
    assert task_set.prompt_for(plain) == plain.prompt


def test_the_chess_pool_is_correct_when_python_chess_is_installed() -> None:
    chess = pytest.importorskip("chess")

    def mates_in_one(board) -> bool:
        for move in list(board.legal_moves):
            board.push(move)
            mate = board.is_checkmate()
            board.pop()
            if mate:
                return True
        return False

    def forces_mate_in_two(board, move) -> bool:
        board.push(move)
        try:
            replies = list(board.legal_moves)
            if board.is_checkmate() or not replies:
                return False
            for reply in replies:
                board.push(reply)
                ok = mates_in_one(board)
                board.pop()
                if not ok:
                    return False
            return True
        finally:
            board.pop()

    for fen, keys in chess_mate_in_2.POOL:
        board = chess.Board(fen)
        assert board.is_valid() and not mates_in_one(board), fen
        found = sorted(m.uci() for m in list(board.legal_moves) if forces_mate_in_two(board, m))
        assert found == sorted(keys), fen


def test_the_benchmark_doc_embeds_the_task_table() -> None:
    doc = (Path(__file__).resolve().parent.parent / "docs" / "BENCHMARK.md").read_text(encoding="utf-8")
    assert task_set.tasks_markdown() in doc, "regenerate the table with `python -m bench tasks`"


def test_bench_never_imports_from_tests() -> None:
    root = Path(__file__).resolve().parent.parent / "bench"
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*(from|import)\s+(tests|server_helpers|verify_helpers|local_sandbox)\b", source, re.M
        ), path
