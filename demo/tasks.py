"""The one demo task that is not in the benchmark: a hand of blackjack played one move at a time, with a take-back (act 4).

The expected answer comes from the reference pack's own game engine (`examples/toolpacks/blackjack_session/tool.py`, imported from the
checkout, never from the sandbox), so the checker and the pack cannot disagree about the rules. The deal depends only on the game seed.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, is_int

ID = "blackjack_hand"
GAME_SEED = (
    2  # the seed in the pack's own examples: a 6 and an ace for the player, a 3 showing for the dealer
)
ENGINE = Path(__file__).resolve().parent.parent / "examples" / "toolpacks" / "blackjack_session" / "tool.py"

PROMPT = """\
A game of blackjack is played one move at a time, and you have no tool for it. Use the shuffle seed {seed}, so the deal is the same for
everyone. Deal a new hand and hit once. Then change your mind: go back to the state right after the deal, and stand instead.

Answer with one JSON object of exactly this shape, from the finished hand:
{{"player_total": <int>, "dealer_total": <int>, "status": "<player_win | dealer_win | push | dealer_bust>"}}"""


@cache
def engine() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_blackjack_engine", ENGINE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {ENGINE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outcome(seed: int = GAME_SEED) -> dict[str, Any]:
    """The finished hand after: deal, then stand (the hit was taken back)."""
    game = engine()._play(seed, ["stand"])
    return {
        "player_total": engine().total(game["player"]),
        "dealer_total": engine().total(game["dealer"]),
        "status": game["status"],
    }


def generate(seed: int, sibling: bool = False) -> Instance:
    """The demo seed does not change the game: the deal is fixed by `GAME_SEED`, so the page of the script never goes stale."""
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(seed=GAME_SEED),
        files={},
        expected=outcome(),
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None:
        return Check(False, "the answer is not a JSON object")
    for key, want in instance.expected.items():
        got = data.get(key)
        if (is_int(want) and (not is_int(got) or got != want)) or (not is_int(want) and got != want):
            return Check(False, f"{key}: expected {want!r}, got {got!r}")
    return Check(True, "the finished hand matches")


TASK = Task(
    id=ID,
    tier="3",
    title="A hand of blackjack with a take-back",
    capability="keep a game alive across calls, snapshot it, and go back one move",
    generate=generate,
    check=check,
    has_sibling=False,
)
