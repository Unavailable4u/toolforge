from tool import ToolError, run


def test_new_game_default():
    result = run({"action": "new_game", "seed": 42})
    assert "state" in result
    assert "legal_actions" in result
    assert "observation" in result
    assert result["state"]["position"] == [0, 0]
    assert result["state"]["grid"] == [
        "SFFF",
        "FHFH",
        "FFFH",
        "HFFG",
    ]


def test_new_game_custom_map():
    result = run({"action": "new_game", "map": "8x8", "seed": 42})
    assert len(result["state"]["grid"]) == 8
    assert len(result["state"]["grid"][0]) == 8


def test_step_action():
    result = run({"action": "new_game", "seed": 42, "is_slippery": False})
    state = result["state"]

    result = run({"action": "step", "direction": "right", "state": state})
    assert "state" in result
    assert result["state"]["position"] == [0, 1]
    assert result["state"]["steps_taken"] == 1


def test_step_into_hole():
    result = run({"action": "new_game", "seed": 42, "is_slippery": False})
    state = result["state"]

    # Move down to (1,0) which is 'F', then down to (2,0) 'F', then down to (3,0) 'H'
    for _ in range(3):
        result = run({"action": "step", "direction": "down", "state": result["state"]})

    assert result["result"] == "lose"
    assert result["state"]["legal_actions"] == []


def test_step_reaches_goal():
    result = run({"action": "new_game", "seed": 42, "is_slippery": False})
    state = result["state"]

    # Path to goal: right, right, right, down, down, down
    moves = ["right", "right", "right", "down", "down", "down"]
    for move in moves:
        result = run({"action": "step", "direction": move, "state": result["state"]})

    assert result["result"] == "win"
    assert result["state"]["position"] == [3, 3]


def test_invalid_action():
    try:
        run({"action": "invalid_action"})
        assert False, "Expected ToolError"
    except ToolError as e:
        assert "unknown action" in str(e)


def test_step_requires_state():
    try:
        run({"action": "step", "direction": "right"})
        assert False, "Expected ToolError"
    except ToolError as e:
        assert "state is required" in str(e)