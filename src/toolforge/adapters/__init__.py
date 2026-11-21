"""Material shipped for the agents that use ToolForge. `recipes/` holds operator-authored guides served by `forge_recipe`.

P07 ships one recipe, `package-format`. Later patches add framework adapters and more recipes here; `recipe_kinds()` lists whatever
the folder holds, so adding a kind is adding a file.
"""

from __future__ import annotations

import re
from importlib import resources

RECIPE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


def _folder():
    return resources.files("toolforge.adapters").joinpath("recipes")


def recipe_kinds() -> list[str]:
    """The kinds of recipe that exist, sorted."""
    return sorted(entry.name[: -len(".md")] for entry in _folder().iterdir() if entry.name.endswith(".md"))


def read_recipe(kind: str) -> str | None:
    """The text of the recipe `kind`, or None when there is no such recipe. `kind` is checked against the list of kinds before any
    file is opened, so it can never be turned into a path."""
    if not isinstance(kind, str) or not RECIPE_NAME.fullmatch(kind) or kind not in recipe_kinds():
        return None
    return _folder().joinpath(f"{kind}.md").read_text(encoding="utf-8")


__all__ = ["read_recipe", "recipe_kinds"]
