"""`blackjack_basic` (tier 3): play hands from a recorded shoe with a given strategy table and report the win rate.

The shoe is a file, so the outcome is fixed: the checker replays the same rules and accepts a win rate within one hand of the true one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, is_int, rng_for

ID = "blackjack_basic"
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
DEALER_UPCARDS = tuple(range(2, 12))  # 11 is an ace

RULES = """\
* Cards are dealt from the top of shoe.txt (one card per line) in this order for each hand: player, dealer, player, dealer.
  The dealer's first card is the upcard. Hands follow each other with no shuffle and no burnt cards.
* Card values: 2 to 10 as written, J/Q/K are 10, A is 11 unless that would take the hand over 21, then 1.
* The player acts first, following strategy.csv: the row is the player's current total (4 to 21), the column the dealer's upcard
  value (2 to 11, where 11 is an ace); 'H' means take a card, 'S' means stand. Keep taking cards while the table says H.
  Over 21 the player busts and loses at once.
* There is no splitting, doubling, insurance or surrender.
* Then the dealer takes cards while the dealer's total is below 17 and stands on any 17 or more.
* The player wins when the dealer busts or when the player's total is higher. Equal totals are a push, which is not a win.
  Two-card 21s get no special treatment."""

PROMPT = """\
You are given shoe.txt and strategy.csv (in inputs/). Play exactly {hands} hands under these rules:

{rules}

Answer with one JSON object of exactly this shape: {{"hands": <int>, "wins": <int>, "win_rate": <wins divided by hands>}}"""


def card_value(card: str) -> int:
    return 11 if card == "A" else 10 if card in ("J", "Q", "K") else int(card)


def total(cards: list[str]) -> int:
    value = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c == "A")
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


def strategy_table(rng) -> dict[tuple[int, int], str]:
    """A plausible basic-strategy-like table with a few seeded deviations, so the table must really be read."""
    table: dict[tuple[int, int], str] = {}
    for player in range(4, 22):
        for up in DEALER_UPCARDS:
            if player >= 17:
                move = "S"
            elif player >= 13:
                move = "S" if up <= 6 else "H"
            elif player == 12:
                move = "S" if 4 <= up <= 6 else "H"
            else:
                move = "H"
            if 12 <= player <= 16 and rng.random() < 0.12:
                move = "S" if move == "H" else "H"
            table[(player, up)] = move
    return table


def play(shoe: list[str], table: dict[tuple[int, int], str], hands: int) -> int:
    """The reference implementation. Returns the number of wins; raises IndexError when the shoe runs out."""
    position, wins = 0, 0

    def draw() -> str:
        nonlocal position
        card = shoe[position]
        position += 1
        return card

    for _ in range(hands):
        player = [draw()]
        dealer = [draw()]
        player.append(draw())
        dealer.append(draw())
        upcard = card_value(dealer[0])
        while total(player) <= 21 and table[(max(4, total(player)), upcard)] == "H":
            player.append(draw())
        if total(player) > 21:
            continue
        while total(dealer) < 17:
            dealer.append(draw())
        if total(dealer) > 21 or total(player) > total(dealer):
            wins += 1
    return wins


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    hands = 60 if not sibling else 50
    shoe = [rank for rank in RANKS for _ in range(4 * 8)]
    rng.shuffle(shoe)
    table = strategy_table(rng)
    while True:
        try:
            wins = play(shoe, table, hands)
            break
        except (
            IndexError
        ):  # this order ran out of cards (rare with 416 cards): shuffle again, still deterministic
            rng.shuffle(shoe)
    csv = ["player," + ",".join(str(up) for up in DEALER_UPCARDS)]
    for player in range(4, 22):
        csv.append(f"{player}," + ",".join(table[(player, up)] for up in DEALER_UPCARDS))
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(hands=hands, rules=RULES),
        files={
            "shoe.txt": ("\n".join(shoe) + "\n").encode("utf-8"),
            "strategy.csv": ("\n".join(csv) + "\n").encode("utf-8"),
        },
        expected={"hands": hands, "wins": wins, "win_rate": wins / hands},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None:
        return Check(False, "the answer is not a JSON object")
    want = instance.expected
    if not is_int(data.get("hands")) or data["hands"] != want["hands"]:
        return Check(False, f"hands: expected {want['hands']}, got {data.get('hands')!r}")
    rate = data.get("win_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return Check(False, f"win_rate is not a number: {rate!r}")
    band = 1 / want["hands"]  # one hand either way
    if abs(rate - want["win_rate"]) > band + 1e-9:
        return Check(False, f"win_rate {rate} is outside {want['win_rate']:.4f} +/- {band:.4f}")
    return Check(True, f"win rate within one hand of {want['win_rate']:.4f}")


TASK = Task(
    id=ID,
    tier="3",
    title="Blackjack with a strategy table",
    capability="simulate a card game from a recorded shoe and a strategy table, and report a rate",
    generate=generate,
    check=check,
    has_sibling=False,
)
