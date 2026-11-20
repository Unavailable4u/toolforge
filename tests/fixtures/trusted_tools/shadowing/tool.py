"""Trusted tool for the shadowing tests."""

from __future__ import annotations

import json

import toolforge_sdk


def run(args: dict) -> object:
    if args["mode"] == "report":
        return {
            "json_is_shadow": hasattr(json, "SHADOW"),
            "sdk_is_shadow": toolforge_sdk.__file__.endswith("shadowing/toolforge_sdk.py"),
        }
    raise toolforge_sdk.ToolError("a real ToolError")
