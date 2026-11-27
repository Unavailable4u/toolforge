"""`chess_mate_in_2` (tier 3): find the first move of a forced mate in two, for three seeded puzzles.

The puzzle pool below was generated and checked with python-chess 1.10.0 (the runtime image's version): for each position white has no
mate in one, and `keys` is every white move after which every black reply allows a mate in one. The checker accepts any key.
`tests/test_bench_tasks.py` re-checks the pool when python-chess is installed and skips that one test otherwise; the host needs no chess
library to judge an answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, rng_for

ID = "chess_mate_in_2"
PUZZLES_PER_TASK = 3
# (FEN, every key move in UCI). White to move in each.
POOL: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("7k/p7/8/2R2RK1/8/8/4p3/8 w - - 0 1", ("g5g6", "g5h6")),
    ("4k3/K2R4/3p4/1R6/8/8/8/8 w - - 0 1", ("d7h7",)),
    ("2Q5/1b2N2k/8/8/8/p7/8/6K1 w - - 0 1", ("c8g8",)),
    ("8/8/3p4/2p4p/8/4Q3/3B3K/k7 w - - 0 1", ("e3b3",)),
    ("8/8/1K3pp1/6Q1/4R3/p7/k7/8 w - - 0 1", ("g5d2",)),
    ("4k3/8/7N/B5K1/3R4/8/8/8 w - - 0 1", ("g5f6",)),
    ("3k4/p7/4K2Q/8/6p1/b7/3p3B/8 w - - 0 1", ("h6h8",)),
    ("8/7R/3N4/p7/4n3/1B3K2/8/4k3 w - - 0 1", ("d6e4",)),
    ("4RQ2/3p4/8/8/Kp6/8/8/1k6 w - - 0 1", ("e8e2", "f8f2")),
    ("8/R7/5p2/3p4/6KB/7N/8/7k w - - 0 1", ("g4g3",)),
    ("3K4/8/8/2Q5/k7/1b6/7B/8 w - - 0 1", ("h2d6",)),
    ("8/k7/8/7K/8/3p3p/5p2/4QR2 w - - 0 1", ("e1b4",)),
    ("6k1/8/8/4Q3/2R5/K7/8/8 w - - 0 1", ("c4c7", "e5e7")),
    ("6k1/4R1p1/2K1p3/6p1/5Q2/8/8/8 w - - 0 1", ("f4e5", "f4f7")),
)

PROMPT = """\
You are given puzzles.txt (in inputs/): three chess positions in FEN, one per line. In each, white is to move and can force
checkmate in two moves (white moves, black replies, white mates), whatever black does. Find white's first move in each, using a
chess library or engine if you like (python-chess is a common one).

Answer with one JSON object of exactly this shape, one move per puzzle in file order, in UCI notation such as "e2e4":
{{"moves": ["<uci>", "<uci>", "<uci>"]}}"""


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    chosen = rng.sample(range(len(POOL)), PUZZLES_PER_TASK)
    fens = [POOL[i][0] for i in chosen]
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT,
        files={"puzzles.txt": ("\n".join(fens) + "\n").encode("utf-8")},
        expected={"keys": [list(POOL[i][1]) for i in chosen], "fens": fens},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    moves = data.get("moves") if data else None
    if not isinstance(moves, list) or len(moves) != PUZZLES_PER_TASK:
        return Check(False, f"the answer needs a 'moves' list of {PUZZLES_PER_TASK} UCI moves")
    for index, (move, keys) in enumerate(zip(moves, instance.expected["keys"], strict=True)):
        if not isinstance(move, str) or move.strip().lower() not in keys:
            return Check(False, f"puzzle {index + 1}: {move!r} is not a key move (keys: {', '.join(keys)})")
    return Check(True, "every first move forces mate in two")


TASK = Task(
    id=ID,
    tier="3",
    title="Three mate-in-two puzzles",
    capability="drive a chess engine or library to find a forced mate in two",
    generate=generate,
    check=check,
    has_sibling=False,
)
