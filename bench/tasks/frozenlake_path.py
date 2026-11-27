"""`frozenlake_path` (tier 3): find a safe path across a seeded Frozen Lake map. The checker walks the path; any safe path passes."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, rng_for

ID = "frozenlake_path"
SIZE = 8
MOVES = {"L": (0, -1), "D": (1, 0), "R": (0, 1), "U": (-1, 0)}
MAX_PATH = SIZE * SIZE

PROMPT = """\
You are given map.txt (in inputs/): an {size}x{size} Frozen Lake grid, one row per line. S is the start (top left), G the goal (bottom
right), F is frozen ice you may stand on and H is a hole. Moves are L, D, R and U (left, down, right, up); the ice is not slippery, so
every move goes where it says. A move off the edge leaves you where you are. Stepping on H ends the walk in failure.

Find a path from S to G that never steps on H, using a search algorithm (the Gymnasium FrozenLake environment uses the same map
format if you want it). Answer with one JSON object of exactly this shape: {{"path": "<moves, for example RRDDLD>"}}"""


def walk(grid: list[str], path: str) -> tuple[bool, str]:
    """The reference walk: (reached the goal safely, why not)."""
    row, col = 0, 0
    for index, move in enumerate(path):
        if move not in MOVES:
            return False, f"move {index + 1} is {move!r}, not one of L D R U"
        dr, dc = MOVES[move]
        row = min(max(row + dr, 0), SIZE - 1)
        col = min(max(col + dc, 0), SIZE - 1)
        if grid[row][col] == "H":
            return False, f"move {index + 1} steps into a hole at row {row + 1}, column {col + 1}"
        if grid[row][col] == "G":
            return (
                (True, "") if index == len(path) - 1 else (False, "the path goes on after reaching the goal")
            )
    return False, "the path does not reach the goal"


def shortest(grid: list[str]) -> str | None:
    start, goal = (0, 0), (SIZE - 1, SIZE - 1)
    seen = {start: ""}
    queue = deque([start])
    while queue:
        row, col = queue.popleft()
        if (row, col) == goal:
            return seen[(row, col)]
        for move, (dr, dc) in MOVES.items():
            nxt = (min(max(row + dr, 0), SIZE - 1), min(max(col + dc, 0), SIZE - 1))
            if nxt not in seen and grid[nxt[0]][nxt[1]] != "H":
                seen[nxt] = seen[(row, col)] + move
                queue.append(nxt)
    return None


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    density = 0.3 if not sibling else 0.25
    while True:
        cells = [["H" if rng.random() < density else "F" for _ in range(SIZE)] for _ in range(SIZE)]
        cells[0][0], cells[SIZE - 1][SIZE - 1] = "S", "G"
        grid = ["".join(row) for row in cells]
        best = shortest(grid)
        if best is not None and len(best) >= SIZE:  # solvable and not trivially open
            break
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(size=SIZE),
        files={"map.txt": ("\n".join(grid) + "\n").encode("utf-8")},
        expected={"grid": grid, "shortest": len(best)},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    path = data.get("path") if data else None
    if not isinstance(path, str):
        return Check(False, "the answer has no 'path' string")
    path = path.strip().upper().replace(" ", "").replace(",", "")
    if not path or len(path) > MAX_PATH:
        return Check(False, f"the path is empty or longer than {MAX_PATH} moves")
    ok, why = walk(instance.expected["grid"], path)
    return Check(
        ok, f"safe path of {len(path)} moves (shortest {instance.expected['shortest']})" if ok else why
    )


TASK = Task(
    id=ID,
    tier="3",
    title="A safe path across Frozen Lake",
    capability="search a grid world for a path that avoids holes",
    generate=generate,
    check=check,
    has_sibling=False,
)
