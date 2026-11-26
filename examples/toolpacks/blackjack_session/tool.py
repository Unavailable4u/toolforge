"""A game of blackjack that lasts across calls, with a seeded shuffle so the same seed always deals the same game.

`action` is `new` (shuffle with `seed` and deal), `hit`, `stand` or `show`. The player sees both own cards and the dealer's first card;
the dealer's other card stays hidden until the hand is over. The dealer draws to 17 and stands on every 17. A two-card 21 pays out at
once (a push if the dealer has one too). The state is only the seed and the actions taken; the hands are rebuilt from them each
call, so a rewind gives the same cards. The shuffle is a small generator written here, not the `random` module, so the deal does
not depend on the Python version.
"""

from __future__ import annotations

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


MASK = (1 << 64) - 1
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
MAX_ACTIONS = 40


def _numbers(seed: int):
    """An endless stream of 64-bit numbers (xorshift64*), the same on every Python."""
    x = (seed * 6364136223846793005 + 1442695040888963407) & MASK or 0x9E3779B97F4A7C15
    while True:
        x ^= x >> 12
        x ^= (x << 25) & MASK
        x ^= x >> 27
        yield (x * 2685821657736338717) & MASK


def _deck(seed: int) -> list[str]:
    cards = [rank for rank in RANKS for _ in range(4)]
    numbers = _numbers(seed)
    for i in range(len(cards) - 1, 0, -1):
        j = next(numbers) % (i + 1)
        cards[i], cards[j] = cards[j], cards[i]
    return cards


def _value(rank: str) -> int:
    return 11 if rank == "A" else 10 if rank in ("10", "J", "Q", "K") else int(rank)


def total(hand: list[str]) -> int:
    """The best total not over 21 (an ace counts 11 until that would bust, then 1)."""
    points = sum(_value(card) for card in hand)
    aces = hand.count("A")
    while points > 21 and aces:
        points -= 10
        aces -= 1
    return points


def _play(seed: int, actions: list[str]) -> dict:
    deck = _deck(seed)
    player, dealer = [deck[0], deck[2]], [deck[1], deck[3]]
    drawn = 4
    status = "playing"
    if total(player) == 21:
        status = "push" if total(dealer) == 21 else "player_blackjack"
    elif total(dealer) == 21:
        status = "dealer_win"
    for action in actions:
        if status != "playing":
            raise ToolError("the game state is damaged; start again with action new")
        if action == "hit":
            player.append(deck[drawn])
            drawn += 1
            if total(player) > 21:
                status = "player_bust"
        else:
            while total(dealer) < 17:
                dealer.append(deck[drawn])
                drawn += 1
            mine, theirs = total(player), total(dealer)
            status = (
                "dealer_bust"
                if theirs > 21
                else "player_win"
                if mine > theirs
                else "dealer_win"
                if theirs > mine
                else "push"
            )
    return {"player": player, "dealer": dealer, "status": status}


def _observe(game: dict) -> dict:
    over = game["status"] != "playing"
    dealer = game["dealer"]
    return {
        "player": game["player"],
        "player_total": total(game["player"]),
        "dealer": dealer if over else dealer[:1],
        "dealer_total": total(dealer) if over else None,
        "status": game["status"],
        "available": [] if over else ["hit", "stand"],
    }


def _state_ok(state: dict) -> bool:
    actions = state.get("actions")
    return (
        isinstance(state.get("seed"), int)
        and not isinstance(state.get("seed"), bool)
        and isinstance(actions, list)
        and all(a in ("hit", "stand") for a in actions)
    )


def run(args: dict, state: dict) -> dict:
    action = args["action"]
    if action == "new":
        state.clear()
        state.update({"seed": args.get("seed", 0), "actions": []})
        return _observe(_play(state["seed"], []))
    if not state:
        raise ToolError("no game yet; start one with action new")
    if not _state_ok(state):
        raise ToolError("the game state is damaged; start again with action new")
    game = _play(state["seed"], state["actions"])
    if action == "show":
        return _observe(game)
    if game["status"] != "playing":
        raise ToolError(f"the hand is over ({game['status']}); start again with action new")
    if len(state["actions"]) >= MAX_ACTIONS:
        raise ToolError("too many actions in one hand; start again with action new")
    state["actions"].append(action)
    return _observe(_play(state["seed"], state["actions"]))
