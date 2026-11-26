from tool import ToolError, run


def play(state, *moves):
    out = None
    for move in moves:
        out = run({"action": "move", "move": move}, state)
    return out


def refused(args, state, needle):
    try:
        run(args, state)
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def test_new_game_shows_the_start_position():
    state = {}
    out = run({"action": "new"}, state)
    assert out["turn"] == "white" and out["status"] == "ongoing" and len(out["legal_moves"]) == 20
    assert out["board"][0] == "r n b q k b n r" and out["fen"].startswith(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w"
    )
    assert state == {"start": out["fen"], "moves": []}


def test_san_and_uci_moves_both_work_and_the_turn_changes():
    state = {}
    run({"action": "new"}, state)
    out = play(state, "e4", "e7e5", "Nf3")
    assert out["turn"] == "black" and out["last_move"] == "Nf3" and state["moves"] == ["e2e4", "e7e5", "g1f3"]


def test_an_illegal_move_is_refused_and_changes_nothing():
    state = {}
    run({"action": "new"}, state)
    refused({"action": "move", "move": "e5"}, state, "not a legal move")
    assert state["moves"] == []


def test_scholars_mate_is_detected_and_ends_the_game():
    state = {}
    run({"action": "new"}, state)
    out = play(state, "e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7#")
    assert (
        out["status"] == "checkmate"
        and out["winner"] == "white"
        and out["result"] == "1-0"
        and out["legal_moves"] == []
    )
    refused({"action": "move", "move": "a6"}, state, "game is over")
    assert run({"action": "show"}, state)["status"] == "checkmate"


def test_stalemate_from_a_fen():
    state = {}
    out = run({"action": "new", "fen": "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"}, state)
    assert out["status"] == "stalemate" and out["result"] == "1/2-1/2" and out["winner"] is None


def test_new_replaces_a_game_in_progress():
    state = {}
    run({"action": "new"}, state)
    play(state, "e4")
    run({"action": "new"}, state)
    assert state["moves"] == []


def test_bad_fen_and_missing_game_and_damaged_state_are_handled_errors():
    refused({"action": "new", "fen": "not a fen"}, {}, "not a valid")
    refused({"action": "new", "fen": "8/8/8/8/8/8/8/8 w - - 0 1"}, {}, "not a legal position")
    refused({"action": "show"}, {}, "start one")
    refused({"action": "show"}, {"start": "x", "moves": "oops"}, "damaged")
    refused(
        {"action": "move", "move": "e4"},
        {"start": run({"action": "new"}, {})["fen"], "moves": ["zz99"]},
        "damaged",
    )


def test_state_replays_to_the_same_position():
    first = {}
    run({"action": "new"}, first)
    played = play(first, "d4", "d5", "c4")
    again = run({"action": "show"}, {"start": first["start"], "moves": list(first["moves"])})
    assert again == played
