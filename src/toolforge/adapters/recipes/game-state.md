# Game‑state recipe

Use this recipe when the agent needs to interact with a state‑based game or simulation.

## Quick example

A chess session tool:

```json
{
  "name": "chess_session",
  "description": "Play chess against an opponent",
  "kind": "game-state",
  "manifest": {
    "session": true
  },
  "tool": {
    "input_schema": {
      "type": "object",
      "properties": {
        "action": {
          "type": "string",
          "description": "Chess move in UCI notation (e.g., 'e2e4') or 'resign'",
          "enum": ["legal_moves", "resign"]
        },
        "fen": {
          "type": "string",
          "description": "Current board position in FEN notation (optional, defaults to starting position)"
        }
      },
      "required": ["action"]
    },
    "engine": "chess",
    "state": {
      "initial": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      "serialize": "to_fen()",
      "deserialize": "from_fen({{fen}})"
    },
    "actions": {
      "legal_moves": {
        "execute": "board.legal_moves",
        "format": "uci"
      },
      "move": {
        "execute": "board.push_uci({{move}})",
        "validate": "move in board.legal_moves"
      },
      "resign": {
        "execute": "board.set_game_result('0-1')"
      }
    },
    "observation": {
      "board": "board.fen()",
      "turn": "board.turn",
      "result": "board.result() if board.is_game_over() else null",
      "legal_moves": ["move.uci() for move in board.legal_moves"]
    }
  },
  "test": {
    "fixtures": [
      {
        "initial_state": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "actions": [
          {"action": "legal_moves"},
          {"action": "move", "move": "e2e4"},
          {"action": "legal_moves"}
        ],
        "expected": {
          "final_turn": "BLACK",
          "legal_moves_count": 20
        }
      }
    ]
  }
}
```

## Engines

The runtime image includes:

* **chess**: Python‑chess library for chess games.
* **gymnasium**: OpenAI Gym environments (FrozenLake, Taxi, etc.).
* **blackjack**: Simple card game implementation.
* Custom engines can be implemented as Python classes.

## State management

Game‑state tools are **session tools** (P13). The tool maintains state across calls:

1. **Initial state**: Starting position or configuration.
2. **Serialize/deserialize**: Functions to convert between Python objects and a string representation.
3. **Action validation**: Each action is checked before execution.
4. **Observation**: The current game state exposed to the caller.

## Actions

Define a vocabulary of actions the tool can perform:

* **Legal moves**: List available moves (required for every turn‑based game).
* **Game actions**: Execute a move, draw a card, place a piece, etc.
* **Meta actions**: Resign, offer draw, restart.

Each action has:
* `execute`: Python code to run.
* `validate`: Optional validation (e.g., move legality).
* `format`: How to present the result.

## Observations

The observation is a JSON‑serializable view of the current game state. Include:

* Board/position representation.
* Current player/agent turn.
* Legal moves (if applicable).
* Game result (if game over).
* Score or rewards (if defined).

## Constraints

* Maximum state size: 1 MB serialized.
* Maximum action execution time: 1 second.
* Maximum session length: 1000 actions.
* Games must be **deterministic** given the same seed and action sequence.

## Writing tests

1. Define initial state(s).
2. List a sequence of actions to test.
3. Specify expected observations or final state properties.
4. Test edge cases (game over, invalid moves, etc.).

## Error handling

Invalid actions produce a `ToolError` with code `"invalid_action"`. Game‑specific errors use `"game_error"`. Timeouts use `"timeout"`.

## Security notes

* Games run in an isolated sandbox.
* No network access or file I/O.
* State is serialized and deserialized through a safe interface.
* Python code in `execute` fields is limited to engine‑specific methods.