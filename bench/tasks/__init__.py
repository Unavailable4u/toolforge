"""The benchmark tasks: the one list (`TASKS`), and the shapes every task shares.

A task is a generator and a **hidden checker**. `generate(seed, sibling)` makes an `Instance`: the prompt the agent reads, the input
files it gets, the data a fixture server publishes (for the tasks that need one) and the expected answer, which never leaves the
harness. `check(instance, answer, files)` judges the agent's final answer, and only that: it never sees the agent's reasoning, its tool
calls or its messages. A sibling instance is the same capability over different generated data, for the reuse experiment.

Generators are deterministic: the same `(seed, sibling)` gives the same instance, byte for byte (`tests/test_bench_tasks.py`). Each one
draws from its own `random.Random` keyed by the task id, the seed and the sibling flag, so adding a task changes no other task's data.

`TASKS` is the single source of truth for the task set. `docs/BENCHMARK.md` embeds `tasks_markdown()` and a drift test compares them.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

TIERS = ("1", "2", "3", "safety")
# What a task needs beyond a sandbox. A run whose need is not met is recorded `not_measured` with the reason, never as a failure.
NEEDS = {
    "fixture_site": "a fixture web server reachable from inside the sandbox through the egress proxy (--fixture-host)",
}
MAX_ANSWER_CHARS = 200_000
# Tasks that read from the fixture server write this placeholder where its address goes; the harness fills it in (`prompt_for`).
FIXTURE_URL = "<<FIXTURE_URL>>"


@dataclass(frozen=True)
class Instance:
    """One generated problem. `expected` and `canary` are for the harness only; nothing the agent sees is derived from them."""

    task_id: str
    seed: int
    sibling: bool
    prompt: str
    files: Mapping[str, bytes]  # name -> content; the agent sees them as `inputs/<name>`
    expected: Any
    served: Mapping[str, tuple[str, bytes]] = field(
        default_factory=dict
    )  # path -> (content type, body) for the fixture server
    allowed_hosts: tuple[str, ...] = ()  # hosts the scripted operator may approve for this instance
    canary: str | None = None  # a string that must never leave the sandbox (the safety task)

    def fingerprint(self) -> str:
        """A stable hash of everything the agent can see plus the expected answer: two equal fingerprints are the same instance."""
        import hashlib

        digest = hashlib.sha256()
        digest.update(self.prompt.encode("utf-8"))
        for name in sorted(self.files):
            digest.update(name.encode("utf-8") + b"\0" + self.files[name])
        for path in sorted(self.served):
            kind, body = self.served[path]
            digest.update(path.encode("utf-8") + b"\0" + kind.encode("utf-8") + b"\0" + body)
        digest.update(json.dumps(self.expected, sort_keys=True, default=str).encode("utf-8"))
        return digest.hexdigest()


@dataclass(frozen=True)
class Check:
    """A checker's verdict. `reason` is one short line for the report; it may name the expected value (the agent never reads it)."""

    ok: bool
    reason: str


@dataclass(frozen=True)
class Task:
    id: str
    tier: str
    title: str
    capability: str  # one line: what a tool for this task has to do
    generate: Callable[[int, bool], Instance]
    check: Callable[[Instance, Any, Mapping[str, bytes]], Check]
    has_sibling: bool = False
    needs: tuple[str, ...] = ()


def prompt_for(instance: Instance, fixture_url: str | None = None) -> str:
    """The prompt as the agent reads it. A task that names the fixture server cannot be given without its address."""
    if FIXTURE_URL not in instance.prompt:
        return instance.prompt
    if not fixture_url:
        raise ValueError(f"{instance.task_id} needs the fixture server's address")
    return instance.prompt.replace(FIXTURE_URL, fixture_url.rstrip("/"))


def rng_for(task_id: str, seed: int, sibling: bool) -> random.Random:
    """The task's own random stream. A string seed is hashed by `random` (SHA-512), the same on every platform and Python 3.12+."""
    return random.Random(f"toolforge-bench:{task_id}:{seed}:{'sibling' if sibling else 'main'}")


def answer_object(answer: Any) -> dict[str, Any] | None:
    """The agent's final answer as a JSON object, or None. A string holding one JSON object (with or without a code fence) is read."""
    if isinstance(answer, dict):
        return answer
    if isinstance(answer, str) and len(answer) <= MAX_ANSWER_CHARS:
        text = answer.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return None
        return value if isinstance(value, dict) else None
    return None


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def answer_text(answer: Any) -> str:
    """Everything the answer says, as text, for leak checks."""
    try:
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return str(answer)


def _load() -> tuple[Task, ...]:
    from bench.tasks import (
        blackjack_basic,
        browser_fixture_headlines,
        chess_mate_in_2,
        fixed_width,
        frozenlake_path,
        hostile_data_file,
        http_mock_fetch,
        log_stats,
        messy_dates,
        sqlite_join,
        tlv_convert,
        yaml_diff,
    )

    return tuple(
        module.TASK
        for module in (
            log_stats,
            sqlite_join,
            tlv_convert,
            fixed_width,
            messy_dates,
            yaml_diff,
            blackjack_basic,
            frozenlake_path,
            chess_mate_in_2,
            http_mock_fetch,
            browser_fixture_headlines,
            hostile_data_file,
        )
    )


TASKS: tuple[Task, ...] = _load()


def task_ids() -> list[str]:
    return [task.id for task in TASKS]


def get(task_id: str) -> Task:
    for task in TASKS:
        if task.id == task_id:
            return task
    raise KeyError(f"there is no benchmark task called {task_id!r}; the tasks are: {', '.join(task_ids())}")


def tasks_markdown() -> str:
    """The task table `docs/BENCHMARK.md` embeds (drift-tested)."""
    rows = ["| Task | Tier | Sibling | Needs | What a tool has to do |", "| --- | --- | --- | --- | --- |"]
    for task in TASKS:
        needs = ", ".join(task.needs) or "sandbox only"
        rows.append(
            f"| `{task.id}` | {task.tier} | {'yes' if task.has_sibling else 'no'} | {needs} | {task.capability} |"
        )
    return "\n".join(rows)


__all__ = [
    "NEEDS",
    "TASKS",
    "TIERS",
    "Check",
    "Instance",
    "Task",
    "answer_object",
    "answer_text",
    "get",
    "is_int",
    "rng_for",
    "task_ids",
    "tasks_markdown",
]
