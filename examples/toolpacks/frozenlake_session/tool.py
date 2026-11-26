"""FrozenLake reinforcement learning environment."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


MAPS = {
    "4x4": [
        "SFFF",
        "FHFH",
        "FFFH",
        "HFFG",
    ],
    "8x8": [
        "SFFFFFFF",
        "FFFFFFFF",
        "FFFHFFFF",
        "FFFFFHFF",
        "FFFHFFFF",
        "FHHFFFHF",
        "FHFFHFHF",
        "FFFHFFFG",
    ],
}

ACTIONS = ["up", "down", "left", "right"]
ACTION_DELTAS = {
    "up": (-1, 0),
    "down": (1, 0),
    "left": (0, -1),
    "right": (0, 1),
}


@dataclass
class FrozenLakeState:
    grid: list[str]
    position: list[int]  # [row, col]
    is_slippery: bool
    seed: int
    max_steps: int
    steps_taken: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid": self.grid,
            "position": self.position,
            "is_slippery": self.is_slippery,
            "seed": self.seed,
            "max_steps": self.max_steps,
            "steps_taken": self.steps_taken,
        }

    @property
    def done(self) -> bool:
        row, col = self.position
        cell = self.grid[row][col]
        return cell in ["G", "H"] or self.steps_taken >= self.max_steps

    @property
    def result(self) -> str | None:
        row, col = self.position
        cell = self.grid[row][col]
        if cell == "G":
            return "win"
        elif cell == "H":
            return "lose"
        elif self.steps_taken >= self.max_steps:
            return "timeout"
        return None


def run(args: dict) -> dict:
    action = args["action"]
    seed = args.get("seed", 42)
    random.seed(seed)

    if action == "new_game":
        map_name = args.get("map", "4x4")
        is_slippery = args.get("is_slippery", True)
        max_steps = args.get("max_steps", 100)

        grid = MAPS.get(map_name, MAPS["4x4"])
        state = FrozenLakeState(
            grid=grid,
            position=[0, 0],
            is_slippery=is_slippery,
            seed=seed,
            max_steps=max_steps,
        )

        return {
            "state": state.to_dict(),
            "legal_actions": ACTIONS,
            "observation": f"Start at (0,0). Goal at ({len(grid)-1},{len(grid[0])-1}). Grid: {len(grid)}x{len(grid[0])}.",
        }

    elif action == "step":
        if "state" not in args:
            raise ToolError("state is required for step action")
        if "direction" not in args:
            raise ToolError("direction is required for step action")

        state_data = args["state"]
        direction = args["direction"]

        state = FrozenLakeState(
            grid=state_data["grid"],
            position=state_data["position"],
            is_slippery=state_data["is_slippery"],
            seed=state_data["seed"],
            max_steps=state_data["max_steps"],
            steps_taken=state_data.get("steps_taken", 0),
        )

        if state.done:
            raise ToolError(f"episode already finished: {state.result}")

        # Determine actual direction (slippery means random action)
        if state.is_slippery and random.random() < 0.33:
            actual_direction = random.choice(ACTIONS)
        else:
            actual_direction = direction

        dr, dc = ACTION_DELTAS[actual_direction]
        new_row = max(0, min(len(state.grid) - 1, state.position[0] + dr))
        new_col = max(0, min(len(state.grid[0]) - 1, state.position[1] + dc))

        state.position = [new_row, new_col]
        state.steps_taken += 1

        obs = f"Moved {actual_direction} to ({new_row},{new_col})."
        cell = state.grid[new_row][new_col]
        if cell == "H":
            obs += " Fell in a hole!"
        elif cell == "G":
            obs += " Reached goal!"

        return {
            "state": state.to_dict(),
            "legal_actions": ACTIONS if not state.done else [],
            "observation": obs,
            "result": state.result,
        }

    else:
        raise ToolError(f"unknown action: {action}")