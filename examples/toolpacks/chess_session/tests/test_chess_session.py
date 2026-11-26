from tool import ToolError, run


def test_new_game_default():
    result = run({"action": "new_game"})
    assert "state" in result
    assert "legal_moves" in result
    assert "observation" in result
    assert result["state"]["fen"] == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert result["state"]["move_number"] == 1
    assert len(result["legal_moves"]) > 0


def test_new_game_custom_fen():
    custom_fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    result = run({"action": "new_game", "fen": custom_fen})
    assert result["state"]["fen"] == custom_fen


def test_move_action():
    result = run({"action": "new_game"})
    state = result["state"]

    result = run({"action": "move", "move": "e4", "state": state})
    assert "state" in result
    assert result["state"]["move_number"] == 1
    assert "b" in result["state"]["fen"]  # Black to move


def test_analyze_action():
    result = run({"action": "new_game"})
    state = result["state"]

    result = run({"action": "analyze", "state": state})
    assert "evaluation" in result
    assert "best_moves" in result
    assert "observation" in result


def test_invalid_action():
    try:
        run({"action": "invalid_action"})
        assert False, "Expected ToolError"
    except ToolError as e:
        assert "unknown action" in str(e)


def test_move_requires_state():
    try:
        run({"action": "move", "move": "e4"})
        assert False, "Expected ToolError"
    except ToolError as e:
        assert "state is required" in str(e)