"""Chess game analysis session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@dataclass
class ChessState:
    fen: str
    move_number: int
    engine: str = "stockfish"
    analysis_depth: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "fen": self.fen,
            "move_number": self.move_number,
            "engine": self.engine,
            "analysis_depth": self.analysis_depth,
        }


# Simplified legal moves for demonstration
COMMON_OPENINGS = ["e4", "d4", "Nf3", "c4", "g3", "f4", "b3", "Nc3"]


def run(args: dict) -> dict:
    action = args["action"]

    if action == "new_game":
        fen = args.get("fen", STARTING_FEN)
        state = ChessState(fen=fen, move_number=1)
        return {
            "state": state.to_dict(),
            "legal_moves": COMMON_OPENINGS,
            "observation": f"White to move. Position: {fen}",
        }

    elif action == "move":
        if "state" not in args:
            raise ToolError("state is required for move action")
        if "move" not in args:
            raise ToolError("move is required for move action")

        state_data = args["state"]
        move = args["move"]

        state = ChessState(
            fen=state_data["fen"],
            move_number=state_data["move_number"],
            engine=state_data.get("engine", "stockfish"),
            analysis_depth=state_data.get("analysis_depth", 3),
        )

        # Simplified: just increment move number and flip side to move
        # In reality this would update the FEN properly
        parts = state.fen.split()
        if len(parts) > 1 and parts[1] == "w":
            new_fen = " ".join(parts[:1] + ["b"] + parts[2:])
        else:
            new_fen = " ".join(parts[:1] + ["w"] + parts[2:])
            state.move_number += 1

        state.fen = new_fen

        return {
            "state": state.to_dict(),
            "legal_moves": COMMON_OPENINGS,
            "observation": f"{'Black' if parts[1] == 'w' else 'White'} to move. Last move: {move}",
        }

    elif action == "analyze":
        if "state" not in args:
            raise ToolError("state is required for analyze action")

        state_data = args["state"]
        return {
            "state": state_data,
            "evaluation": "+0.15",
            "best_moves": COMMON_OPENINGS[:3],
            "observation": f"Position evaluated at +0.15 for White. Depth: {state_data.get('analysis_depth', 3)}",
        }

    else:
        raise ToolError(f"unknown action: {action}")