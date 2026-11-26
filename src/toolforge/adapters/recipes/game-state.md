# Recipe: a tool that plays a game or keeps state between calls

This is documentation written by the operator of this ToolForge, not output from a tool. Read `package-format` first. A tool that plays
a game, steps through a simulator or drives a program needs to remember where it is. That is a **session tool**: `"kind": "session"`.

## How a session works

- The entrypoint has the signature `run(args: dict, state: dict) -> object`. The harness loads `state` before the call and saves it
  after a call that succeeds. It must be JSON and at most 256 KB.
- `forge_run` on a session tool, without `session_id`, opens a session and returns its `session_id`; pass the same one on later calls.
  `forge_session_rewind(session_id, step)` goes back to the state after an earlier call, and `forge_session_close` ends it.
- **The package is put back before every call.** Keep nothing in module variables or files: everything you need is in `state`.
- An example list is one session, in order: example 2 runs after example 1 on the same state. Gate G3 also runs the first example again
  in a fresh sandbox, so a tool whose first answer depends on anything but `state` is rejected.

## Design the state first

1. **Store the position, or store what it takes to rebuild it.** Small, plain positions (a board, a counter) go in `state` as they are.
   When the game has randomness or an engine, store the **inputs** (a seed, a start position, the list of moves) and rebuild on every
   call by replaying them. A replay is deterministic, so a rewind gives exactly the same position, and the state stays small.
2. **No hidden randomness.** Never call `random` without a seed that is in `state`. If you want the same shuffle on every Python version,
   write a tiny generator (a few lines of xorshift) instead of relying on the `random` module.
3. **Validate `state` on entry.** It is the one input you wrote yourself, but a rewind or a bug can leave it odd; check its shape and
   raise `ToolError("the game state is damaged; start again with action new")` rather than crashing.
4. **Make an `action` the first field of the input**: `new` (replaces the state), `show` (reads it), and the moves. Refuse a move after
   the game is over or before one was started, with a `ToolError` that says what to do next.
5. **Answer with the whole observation**: the board as text lines, whose turn it is, the legal moves, and whether the game is over and
   who won. The agent only sees what you return.

## Libraries in the image

`python-chess` (`chess`) for chess rules and `gymnasium` for the toy-text environments (FrozenLake, Taxi, Blackjack, CliffWalking) are
in the image, with `numpy`. `forge_runtime_info` lists the versions. Create the environment inside `run` (`gymnasium.make(...)`), call
`env.reset(seed=...)`, and replay the stored actions. Do not render with a window or `pygame`: return text.

## Tests

Test the rules with plain calls on a `state` dictionary: `state = {}`, then `run({...}, state)` several times, asserting on the values
returned and on `state`. Include: a full game to a win, a draw or a loss; an illegal move that changes nothing; a call before `new`; a
damaged state; and **replay**: `run({"action": "show"}, copy_of_state)` returns exactly the observation the last move returned.

## A complete small package: tic-tac-toe for two sides

```json manifest
{
  "name": "tictactoe_session",
  "version": "1.0.0",
  "kind": "session",
  "description": "Play tic-tac-toe across calls for both sides: new starts a game, place puts the next mark in a cell 0 to 8, show looks at the board.",
  "when_to_use": "The task is to play or analyse a tic-tac-toe game and the board must be remembered between calls.",
  "entrypoint": "tool.py:run",
  "input_schema": {
    "type": "object",
    "properties": {
      "action": {"type": "string", "enum": ["new", "place", "show"]},
      "cell": {"type": "integer", "minimum": 0, "maximum": 8}
    },
    "required": ["action"],
    "additionalProperties": false
  },
  "examples": [
    {"input": {"action": "new"}, "output": {"board": ["...", "...", "..."], "turn": "X", "winner": null, "legal": [0, 1, 2, 3, 4, 5, 6, 7, 8]}},
    {"input": {"action": "place", "cell": 4}, "output": {"board": ["...", ".X.", "..."], "turn": "O", "winner": null, "legal": [0, 1, 2, 3, 5, 6, 7, 8]}}
  ]
}
```

```python tool.py
from toolforge_sdk import ToolError

LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def _winner(cells):
    for a, b, c in LINES:
        if cells[a] != "." and cells[a] == cells[b] == cells[c]:
            return cells[a]
    return "draw" if "." not in cells else None


def _view(cells):
    winner = _winner(cells)
    over = winner is not None
    turn = None if over else ("X" if cells.count("X") == cells.count("O") else "O")
    return {
        "board": [cells[0:3], cells[3:6], cells[6:9]],
        "turn": turn,
        "winner": winner,
        "legal": [] if over else [i for i, mark in enumerate(cells) if mark == "."],
    }


def run(args, state):
    action = args["action"]
    if action == "new":
        state.clear()
        state["cells"] = "." * 9
        return _view(state["cells"])
    cells = state.get("cells")
    if cells is None:
        raise ToolError("no game yet; start one with action new")
    if not (isinstance(cells, str) and len(cells) == 9 and set(cells) <= set("XO.")):
        raise ToolError("the game state is damaged; start again with action new")
    if action == "show":
        return _view(cells)
    view = _view(cells)
    if view["turn"] is None:
        raise ToolError("the game is over; start again with action new")
    if "cell" not in args or args["cell"] not in view["legal"]:
        raise ToolError("place needs a free cell; legal cells: " + ", ".join(map(str, view["legal"])))
    cell = args["cell"]
    state["cells"] = cells[:cell] + view["turn"] + cells[cell + 1 :]
    return _view(state["cells"])
```

```python tests/test_tictactoe_session.py
from tool import ToolError, run


def refused(args, state, needle):
    try:
        run(args, state)
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def play(state, *cells):
    out = None
    for cell in cells:
        out = run({"action": "place", "cell": cell}, state)
    return out


def test_a_game_to_a_win_and_nothing_after_it():
    state = {}
    run({"action": "new"}, state)
    out = play(state, 0, 3, 1, 4, 2)
    assert out["winner"] == "X" and out["turn"] is None and out["legal"] == [] and out["board"][0] == "XXX"
    refused({"action": "place", "cell": 8}, state, "game is over")


def test_a_full_board_without_a_line_is_a_draw():
    state = {}
    run({"action": "new"}, state)
    out = play(state, 0, 1, 2, 4, 3, 5, 7, 6, 8)
    assert out["winner"] == "draw"


def test_a_taken_cell_is_refused_and_changes_nothing():
    state = {}
    run({"action": "new"}, state)
    play(state, 4)
    refused({"action": "place", "cell": 4}, state, "legal cells")
    assert state["cells"] == "....X...."


def test_no_game_and_a_damaged_state_are_handled_errors_and_show_replays():
    refused({"action": "show"}, {}, "start one")
    refused({"action": "show"}, {"cells": "XX"}, "damaged")
    state = {}
    run({"action": "new"}, state)
    last = play(state, 0, 4)
    assert run({"action": "show"}, {"cells": state["cells"]}) == last
```
