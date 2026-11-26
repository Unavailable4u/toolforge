"""Blackjack game simulation."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


SUITS = ["H", "D", "C", "S"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


@dataclass
class GameState:
    player_hand: list[str]
    dealer_hand: list[str]
    deck: list[str]
    turn: str  # "player", "dealer", "done"
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_hand": self.player_hand,
            "dealer_hand": self.dealer_hand,
            "turn": self.turn,
            "seed": self.seed,
        }


def _create_deck(seed: int) -> list[str]:
    random.seed(seed)
    deck = [f"{rank}{suit}" for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    return deck


def _card_value(card: str) -> int:
    rank = card[:-1]
    if rank in ["J", "Q", "K"]:
        return 10
    elif rank == "A":
        return 11
    else:
        return int(rank)


def _hand_value(hand: list[str]) -> int:
    total = sum(_card_value(c) for c in hand)
    aces = sum(1 for c in hand if c.startswith("A"))
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _is_blackjack(hand: list[str]) -> bool:
    return len(hand) == 2 and _hand_value(hand) == 21


def run(args: dict) -> dict:
    action = args["action"]
    seed = args.get("seed", 42)

    if action == "new_game":
        deck = _create_deck(seed)
        player_hand = [deck.pop(), deck.pop()]
        dealer_hand = [deck.pop(), deck.pop()]

        # Check for blackjack
        if _is_blackjack(player_hand) and not _is_blackjack(dealer_hand):
            return {
                "state": GameState(player_hand, dealer_hand, deck, "done", seed).to_dict(),
                "legal_actions": [],
                "observation": f"Your hand: {' '.join(player_hand)} (21). Dealer shows: {dealer_hand[0]} ??",
                "result": "win",
            }
        elif _is_blackjack(dealer_hand) and not _is_blackjack(player_hand):
            return {
                "state": GameState(player_hand, dealer_hand, deck, "done", seed).to_dict(),
                "legal_actions": [],
                "observation": f"Dealer has blackjack: {dealer_hand[0]} {dealer_hand[1]}. You lose.",
                "result": "lose",
            }
        elif _is_blackjack(player_hand) and _is_blackjack(dealer_hand):
            return {
                "state": GameState(player_hand, dealer_hand, deck, "done", seed).to_dict(),
                "legal_actions": [],
                "observation": "Both have blackjack. Push.",
                "result": "push",
            }

        return {
            "state": GameState(player_hand, dealer_hand, deck, "player", seed).to_dict(),
            "legal_actions": ["hit", "stand", "double", "split"],
            "observation": f"Your hand: {' '.join(player_hand)} ({_hand_value(player_hand)}). Dealer shows: {dealer_hand[0]}",
        }

    elif action in ["hit", "stand", "double", "split"]:
        # In a real implementation, we'd restore state from somewhere
        # For this example, we require the full state to be passed
        if "state" not in args:
            raise ToolError("state is required for actions other than new_game")

        state_data = args["state"]
        player_hand = state_data["player_hand"]
        dealer_hand = state_data["dealer_hand"]
        deck = state_data.get("deck", _create_deck(state_data["seed"]))
        turn = state_data["turn"]
        seed = state_data["seed"]

        if turn != "player":
            raise ToolError(f"not your turn: {turn}")

        if action == "hit":
            player_hand.append(deck.pop())
            if _hand_value(player_hand) > 21:
                return {
                    "state": GameState(player_hand, dealer_hand, deck, "done", seed).to_dict(),
                    "legal_actions": [],
                    "observation": f"Bust! Your hand: {' '.join(player_hand)} ({_hand_value(player_hand)})",
                    "result": "lose",
                }
            return {
                "state": GameState(player_hand, dealer_hand, deck, "player", seed).to_dict(),
                "legal_actions": ["hit", "stand", "double", "split"],
                "observation": f"Your hand: {' '.join(player_hand)} ({_hand_value(player_hand)}). Dealer shows: {dealer_hand[0]}",
            }

        elif action == "stand":
            # Dealer plays
            while _hand_value(dealer_hand) < 17:
                dealer_hand.append(deck.pop())

            player_val = _hand_value(player_hand)
            dealer_val = _hand_value(dealer_hand)

            if dealer_val > 21 or player_val > dealer_val:
                result = "win"
                obs = f"Dealer: {' '.join(dealer_hand)} ({dealer_val}). You win with {player_val}!"
            elif player_val < dealer_val:
                result = "lose"
                obs = f"Dealer: {' '.join(dealer_hand)} ({dealer_val}). You lose with {player_val}."
            else:
                result = "push"
                obs = f"Dealer: {' '.join(dealer_hand)} ({dealer_val}). Push with {player_val}."

            return {
                "state": GameState(player_hand, dealer_hand, deck, "done", seed).to_dict(),
                "legal_actions": [],
                "observation": obs,
                "result": result,
            }

        else:
            raise ToolError(f"action {action} not fully implemented in example")

    else:
        raise ToolError(f"unknown action: {action}")