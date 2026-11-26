"""A FrozenLake grid world that lasts across calls (gymnasium's toy-text environment), moved one step at a time.

`action` is `new` (start; choose `map` 4x4 or 8x8, `slippery` and a `seed`), `move` (with `direction`: left, down, right or up) or `show`.
The grid is drawn as text: `S` start, `F` frozen (safe), `H` hole (the episode ends, reward 0), `G` goal (the episode ends, reward 1),
and `P` is the player. On slippery ice a move may slide sideways; with the same seed the slips are the same. The state is the settings and
the moves taken; the environment is rebuilt and replayed on every call, so a rewind gives the same position. `gymnasium` is in the
runtime image.
"""

from __future__ import annotations

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


import gymnasium

DIRECTIONS = {"left": 0, "down": 1, "right": 2, "up": 3}
MAPS = ("4x4", "8x8")
MAX_MOVES = 200


def _state_ok(state: dict) -> bool:
    moves = state.get("moves")
    return (
        state.get("map") in MAPS
        and isinstance(state.get("slippery"), bool)
        and isinstance(state.get("seed"), int)
        and not isinstance(state.get("seed"), bool)
        and isinstance(moves, list)
        and all(m in DIRECTIONS for m in moves)
    )


def _replay(state: dict) -> dict:
    env = gymnasium.make("FrozenLake-v1", map_name=state["map"], is_slippery=state["slippery"])
    try:
        position, _ = env.reset(seed=state["seed"])
        reward, terminated, truncated = 0.0, False, False
        for move in state["moves"]:
            if terminated or truncated:
                raise ToolError("the game state is damaged; start again with action new")
            position, reward, terminated, truncated, _ = env.step(DIRECTIONS[move])
        cells = [[c.decode() for c in row] for row in env.unwrapped.desc.tolist()]
        size = len(cells)
    finally:
        env.close()
    row, col = divmod(int(position), size)
    return {
        "cells": cells,
        "row": row,
        "col": col,
        "reward": float(reward),
        "terminated": terminated,
        "truncated": truncated,
    }


def _observe(game: dict, steps: int) -> dict:
    cells, row, col = game["cells"], game["row"], game["col"]
    grid = [
        "".join("P" if (r, c) == (row, col) else cell for c, cell in enumerate(line))
        for r, line in enumerate(cells)
    ]
    done = game["terminated"] or game["truncated"]
    if game["truncated"]:
        outcome = "timeout"
    elif game["terminated"]:
        outcome = "won" if cells[row][col] == "G" else "fell"
    else:
        outcome = "playing"
    return {
        "grid": grid,
        "position": [row, col],
        "steps": steps,
        "done": done,
        "reward": game["reward"],
        "outcome": outcome,
        "directions": [] if done else list(DIRECTIONS),
    }


def run(args: dict, state: dict) -> dict:
    action = args["action"]
    if action == "new":
        state.clear()
        state.update(
            {
                "map": args.get("map", "4x4"),
                "slippery": args.get("slippery", False),
                "seed": args.get("seed", 0),
                "moves": [],
            }
        )
        return _observe(_replay(state), 0)
    if not state:
        raise ToolError("no game yet; start one with action new")
    if not _state_ok(state):
        raise ToolError("the game state is damaged; start again with action new")
    game = _replay(state)
    if action == "show":
        return _observe(game, len(state["moves"]))
    if game["terminated"] or game["truncated"]:
        raise ToolError(f"the episode is over ({_observe(game, 0)['outcome']}); start again with action new")
    if "direction" not in args:
        raise ToolError("action move needs a direction: left, down, right or up")
    if len(state["moves"]) >= MAX_MOVES:
        raise ToolError("too many moves; start again with action new")
    state["moves"].append(args["direction"])
    return _observe(_replay(state), len(state["moves"]))
