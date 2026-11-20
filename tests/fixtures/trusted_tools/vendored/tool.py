"""Trusted tool that imports a module from pkg/.deps, where a package's vendored requirements live."""

from __future__ import annotations

import vendored_mod


def run(args: dict) -> object:
    return {"answer": vendored_mod.ANSWER}
