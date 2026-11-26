from tool import ToolError, run

# The 4x4 map is  SFFF / FHFH / FFFH / HFFG. Along the safe path: down, down, right, down, right, right.
SAFE = ["down", "down", "right", "down", "right", "right"]


def refused(args, state, needle):
    try:
        run(args, state)
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def walk(state, moves):
    out = None
    for direction in moves:
        out = run({"action": "move", "direction": direction}, state)
    return out


def test_new_shows_the_grid_with_the_player_on_the_start():
    state = {}
    out = run({"action": "new"}, state)
    assert out["grid"] == ["PFFF", "FHFH", "FFFH", "HFFG"] and out["position"] == [0, 0]
    assert (
        out["outcome"] == "playing"
        and out["done"] is False
        and out["directions"] == ["left", "down", "right", "up"]
    )


def test_a_move_changes_the_position_and_walls_keep_the_player_in_place():
    state = {}
    run({"action": "new"}, state)
    assert run({"action": "move", "direction": "right"}, state)["position"] == [0, 1]
    assert run({"action": "move", "direction": "up"}, state)["position"] == [0, 1]


def test_reaching_the_goal_wins():
    state = {}
    run({"action": "new"}, state)
    out = walk(state, SAFE)
    assert (
        out["outcome"] == "won" and out["reward"] == 1.0 and out["done"] is True and out["directions"] == []
    )
    assert out["steps"] == 6 and out["grid"][3] == "HFFP"


def test_a_hole_ends_the_episode_with_no_reward():
    state = {}
    run({"action": "new"}, state)
    out = walk(state, ["right", "down"])
    assert out["outcome"] == "fell" and out["reward"] == 0.0 and out["done"] is True
    refused({"action": "move", "direction": "up"}, state, "episode is over")


def test_slippery_ice_is_repeatable_with_the_same_seed():
    def trail(seed):
        state = {}
        run({"action": "new", "slippery": True, "seed": seed}, state)
        positions = []
        for direction in ["right", "down", "right", "down"]:
            out = run({"action": "move", "direction": direction}, state)
            positions.append(out["position"])
            if out["done"]:
                break
        return positions

    assert trail(3) == trail(3)


def test_the_8x8_map_is_available():
    out = run({"action": "new", "map": "8x8"}, {})
    assert len(out["grid"]) == 8 and out["grid"][0] == "PFFFFFFF"


def test_state_replays_to_the_same_position():
    state = {}
    run({"action": "new"}, state)
    played = walk(state, ["right", "right", "down"])
    assert run({"action": "show"}, {**state, "moves": list(state["moves"])}) == played


def test_missing_game_missing_direction_and_damaged_state_are_handled_errors():
    refused({"action": "move", "direction": "up"}, {}, "start one")
    state = {}
    run({"action": "new"}, state)
    refused({"action": "move"}, state, "needs a direction")
    refused({"action": "show"}, {"map": "4x4", "slippery": False, "seed": 0, "moves": ["jump"]}, "damaged")
    refused({"action": "show"}, {"map": "9x9", "slippery": False, "seed": 0, "moves": []}, "damaged")
