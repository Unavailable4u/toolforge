"""A chess game that lasts across calls, played by the caller for both sides, with python-chess doing the rules.

`action` is `new` (start, optionally from a FEN), `move` (a move in SAN such as `Nf3` or UCI such as `g1f3`) or `show` (look at the
position). Every answer is the position: the board as text, the FEN, whose turn it is, the legal moves, and whether the game is over.
The state holds only the starting FEN and the moves played, as UCI text; the board is rebuilt from them on every call, so a rewind
or a restarted sandbox gives exactly the same position. `python-chess` is in the runtime image.
"""

from __future__ import annotations

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


import chess

MAX_LEGAL = 100
MAX_MOVES = 600  # a game this long is stopped; the state stays small


def _state_ok(state: dict) -> bool:
    moves = state.get("moves")
    return (
        isinstance(state.get("start"), str)
        and isinstance(moves, list)
        and all(isinstance(m, str) for m in moves)
    )


def _replay(state: dict) -> tuple[chess.Board, str | None]:
    """The board after the stored moves, and the last move in SAN (None before the first move)."""
    if not _state_ok(state):
        raise ToolError("the game state is damaged; start again with action new")
    board = chess.Board(state["start"])
    last = None
    try:
        for uci in state["moves"]:
            move = chess.Move.from_uci(uci)
            last = board.san(move)
            board.push(move)
    except ValueError:
        raise ToolError("the game state is damaged; start again with action new") from None
    return board, last


def _observe(board: chess.Board, last: str | None) -> dict:
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        status = "ongoing"
    elif outcome.termination == chess.Termination.CHECKMATE:
        status = "checkmate"
    elif outcome.termination == chess.Termination.STALEMATE:
        status = "stalemate"
    else:
        status = "draw"
    legal = sorted(board.san(move) for move in board.legal_moves)
    return {
        "board": str(board).split("\n"),
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "last_move": last,
        "in_check": board.is_check(),
        "legal_moves": legal[:MAX_LEGAL],
        "status": status,
        "result": board.result(claim_draw=False),
        "winner": None
        if outcome is None or outcome.winner is None
        else ("white" if outcome.winner else "black"),
    }


def _parse(board: chess.Board, text: str) -> chess.Move:
    text = text.strip()
    for read in (board.parse_san, board.parse_uci):
        try:
            return read(text)
        except ValueError:
            continue
    some = ", ".join(sorted(board.san(m) for m in board.legal_moves)[:12])
    raise ToolError(f"{text[:20]!r} is not a legal move here; some legal moves: {some}")


def run(args: dict, state: dict) -> dict:
    action = args["action"]
    if action == "new":
        fen = args.get("fen", chess.STARTING_FEN)
        try:
            board = chess.Board(fen)
        except ValueError:
            raise ToolError("fen is not a valid chess position") from None
        if not board.is_valid():
            raise ToolError("fen is not a legal position (check the kings, the pawns and whose turn it is)")
        state.clear()
        state.update({"start": board.fen(), "moves": []})
        return _observe(board, None)
    if not state:
        raise ToolError("no game yet; start one with action new")
    board, last = _replay(state)
    if action == "show":
        return _observe(board, last)
    if board.is_game_over():
        raise ToolError("the game is over; start again with action new")
    if "move" not in args:
        raise ToolError("action move needs a move, for example Nf3 or g1f3")
    if len(state["moves"]) >= MAX_MOVES:
        raise ToolError(f"the game reached {MAX_MOVES} moves; start again with action new")
    move = _parse(board, args["move"])
    last = board.san(move)
    board.push(move)
    state["moves"].append(move.uci())
    return _observe(board, last)
