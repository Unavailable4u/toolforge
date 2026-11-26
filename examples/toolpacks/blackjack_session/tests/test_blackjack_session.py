from tool import ToolError, run


def test_new_game_basic():
    result = run({"action": "new_game", "seed": 42})
    assert "state" in result
    assert "legal_actions" in result
    assert "observation" in result
    assert result["state"]["turn"] == "player"
    assert len(result["state"]["player_hand"]) == 2
    assert len(result["state"]["dealer_hand"]) == 2


def test_new_game_deterministic():
    result1 = run({"action": "new_game", "seed": 42})
    result2 = run({"action": "new_game", "seed": 42})
    assert result1["state"]["player_hand"] == result2["state"]["player_hand"]
    assert result1["state"]["dealer_hand"] == result2["state"]["dealer_hand"]


def test_stand_action():
    result = run({"action": "new_game", "seed": 1})
    state = result["state"]

    # Player stands
    result = run({"action": "stand", "state": state})
    assert result["state"]["turn"] == "done"
    assert "result" in result
    assert result["result"] in ["win", "lose", "push"]
    assert len(result["legal_actions"]) == 0


def test_hit_action():
    result = run({"action": "new_game", "seed": 1})
    state = result["state"]

    # Player hits
    result = run({"action": "hit", "state": state})
    assert "state" in result
    assert len(result["state"]["player_hand"]) == 3


def test_invalid_action():
    try:
        run({"action": "invalid_action"})
        assert False, "Expected ToolError"
    except ToolError as e:
        assert "unknown action" in str(e)


def test_hit_bust():
    # Use a seed where hitting will bust
    # We'll just test that the mechanism works
    result = run({"action": "new_game", "seed": 100})
    state = result["state"]

    # Keep hitting until bust or done
    while result["state"]["turn"] == "player":
        result = run({"action": "hit", "state": result["state"]})

    assert result["state"]["turn"] == "done"