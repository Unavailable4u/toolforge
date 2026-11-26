"""Material shipped for the agents that use ToolForge. `recipes/` holds operator-authored guides served by `forge_recipe`.

`RECIPE_KINDS` is the list of recipes this release serves, and `tests/test_recipes.py` checks that it matches the files in `recipes/`
exactly: adding a kind is adding a file and one name here. Every recipe's complete example package is built and run through the real
gates by that test, so a recipe cannot teach something that no longer passes.
"""

from __future__ import annotations

import re
from importlib import resources

RECIPE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")

# Every recipe `forge_recipe` serves.
RECIPE_KINDS: tuple[str, ...] = (
    "browser-plan",
    "file-format",
    "game-state",
    "http-api",
    "package-format",
    "sql",
)


def _folder():
    return resources.files("toolforge.adapters").joinpath("recipes")


def recipe_kinds() -> list[str]:
    """The kinds of recipe that exist, sorted."""
    return sorted(RECIPE_KINDS)


def read_recipe(kind: str) -> str | None:
    """The text of the recipe `kind`, or None when there is no such recipe. `kind` is checked against the list of kinds before any
    file is opened, so it can never be turned into a path."""
    if not isinstance(kind, str) or not RECIPE_NAME.fullmatch(kind) or kind not in recipe_kinds():
        return None
    return _folder().joinpath(f"{kind}.md").read_text(encoding="utf-8")


__all__ = ["RECIPE_KINDS", "read_recipe", "recipe_kinds"]
