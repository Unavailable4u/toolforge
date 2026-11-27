"""The Forge Attack Lab (P15): hostile tool authors against the real pipeline and runtime.

`catalog.py` says what each attack is, how it is driven and which observations prove the defence held; `runner.py` runs the
catalogue sequentially against a throwaway `TOOLFORGE_HOME` and turns the observations into verdicts. See `docs/ATTACKS.md`.

Importing this package never imports MiniLocker, Docker or any tool code.
"""

from toolforge.attacks.catalog import (
    ATTACKS,
    BREACHED,
    CONTAINED,
    INCONCLUSIVE,
    SKIPPED,
    Attack,
    attack_ids,
    attacks_markdown,
    get_attack,
)

__all__ = [
    "ATTACKS",
    "BREACHED",
    "CONTAINED",
    "INCONCLUSIVE",
    "SKIPPED",
    "Attack",
    "attack_ids",
    "attacks_markdown",
    "get_attack",
]
