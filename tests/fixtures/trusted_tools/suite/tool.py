"""A trusted tool and its suite, run only by tests/test_harness_modes.py through the real harness (`--tests`, `--stub`, `--batch`)."""

from __future__ import annotations

import os
import time

from toolforge_sdk import ToolError


def helper(value: int) -> int:
    return value + 1


def run(args: dict) -> dict:
    mode = args.get("mode", "ok")
    if mode == "ok":
        return {"n": args.get("n", 0) * 2}
    if mode == "tool_error":
        raise ToolError("not allowed")
    if mode == "crash":
        return {}["missing"]
    if mode == "sleep":
        time.sleep(args["seconds"])
        return {}
    if mode == "exit":
        os._exit(3)
    if mode == "read":
        with open(args["path"], encoding="utf-8") as handle:
            return {"text": handle.read()}
    if mode == "count_files":
        return {"files": sorted(os.listdir("inputs")) if os.path.isdir("inputs") else []}
    if mode == "big":
        return "x" * args["size"]
    if mode == "child":  # starts a process that outlives the call unless the harness kills the group
        os.spawnlp(os.P_NOWAIT, "sleep", "sleep", "37")
        return {}
    if mode == "write":
        with open("leftover.txt", "w", encoding="utf-8") as handle:
            handle.write("x")
        return {}
    return {}
