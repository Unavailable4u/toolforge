from tool import ToolError, _deck, run, total


def refused(args, state, needle):
    try:
        run(args, state)
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def finish(seed, *actions):
    state = {}
    out = run({"action": "new", "seed": seed}, state)
    for action in actions:
        out = run({"action": action}, state)
    return out, state


def test_totals_count_aces_as_eleven_until_that_busts():
    assert total(["A", "K"]) == 21 and total(["A", "A", "9"]) == 21 and total(["A", "K", "5"]) == 16
    assert total(["10", "6", "7"]) == 23


def test_the_same_seed_deals_the_same_game_and_other_seeds_differ():
    assert _deck(7) == _deck(7)
    assert sorted(_deck(7)) == sorted(_deck(8)) and _deck(7) != _deck(8)
    assert run({"action": "new", "seed": 7}, {}) == run({"action": "new", "seed": 7}, {})


def test_the_dealers_second_card_is_hidden_until_the_hand_ends():
    for seed in range(40):
        out, _ = finish(seed)
        if out["status"] == "playing":
            assert (
                len(out["dealer"]) == 1
                and out["dealer_total"] is None
                and out["available"] == ["hit", "stand"]
            )
            return
    raise AssertionError("no seed in range gave a hand in progress")


def test_standing_ends_the_hand_and_the_dealer_draws_to_seventeen():
    for seed in range(40):
        out, _ = finish(seed)
        if out["status"] != "playing":
            continue
        done, _ = finish(seed, "stand")
        assert done["status"] in ("player_win", "dealer_win", "dealer_bust", "push")
        assert done["available"] == [] and len(done["dealer"]) >= 2
        assert done["dealer_total"] >= 17 or done["dealer_total"] > done["player_total"]
        return
    raise AssertionError("no playable seed")


def test_hitting_until_bust_loses():
    for seed in range(40):
        out, state = finish(seed)
        if out["status"] != "playing":
            continue
        while out["status"] == "playing":
            out = run({"action": "hit"}, state)
        if out["status"] == "player_bust":
            assert out["player_total"] > 21 and out["dealer_total"] is not None
            return
    raise AssertionError("no seed busted")


def test_every_outcome_is_reachable_and_rewinds_replay_identically():
    seen = set()
    for seed in range(300):
        out, state = finish(seed)
        seen.add(out["status"])
        if out["status"] == "playing":
            seen.add(finish(seed, "stand")[0]["status"])
        again = run({"action": "show"}, {"seed": state["seed"], "actions": list(state["actions"])})
        assert again == out
    assert {"player_blackjack", "dealer_win", "player_win", "dealer_bust", "push"} <= seen


def test_acting_after_the_hand_or_without_a_game_is_refused():
    refused({"action": "hit"}, {}, "start one")
    out, state = finish(next(s for s in range(40) if finish(s)[0]["status"] == "playing"), "stand")
    refused({"action": "hit"}, state, "hand is over")
    refused({"action": "show"}, {"seed": "x", "actions": []}, "damaged")
    refused({"action": "show"}, {"seed": 1, "actions": ["fold"]}, "damaged")
