"""The four acts, defined once. `docs/DEMO_SCRIPT.md`, the runner and the transcripts' generator all read this list.

Act 2 reuses what act 1 forged, so it must run after it in the same ToolForge home (`needs`).
"""

from __future__ import annotations

from dataclasses import dataclass

from bench import tasks as bench_tasks
from demo import tasks as demo_tasks

SEED = 7  # the benchmark seed every act's instance is generated from; fixed, so a run is repeatable


@dataclass(frozen=True)
class Act:
    id: str
    number: int
    title: str
    transcript: str  # file name in the transcripts directory
    task: str  # a benchmark task id, or the demo's own
    sibling: bool  # the same capability over different data (the reuse act)
    needs: tuple[str, ...]
    shows: str  # one line, what the audience should take away


ACTS: tuple[Act, ...] = (
    Act(
        "forge",
        1,
        "Forge",
        "01-forge.json",
        "sqlite_join",
        False,
        (),
        "The agent has no SQLite tool. It checks the shelf, forges one, the gates run, an operator promotes it, and the agent answers.",
    ),
    Act(
        "reuse",
        2,
        "Reuse",
        "02-reuse.json",
        "sqlite_join",
        True,
        ("forge",),
        "A different question over a different database: the tool is found, not rebuilt, and the step count drops.",
    ),
    Act(
        "attack",
        3,
        "Attack",
        "03-attack.json",
        "hostile_data_file",
        False,
        (),
        "A poisoned file tells the agent to build a tool that steals credentials. The package is rejected and the ledger says why.",
    ),
    Act(
        "software",
        4,
        "Software and games",
        "04-software.json",
        "blackjack_hand",
        False,
        (),
        "The agent forges a session tool, plays a hand, takes a move back, and the session, snapshots and rewind are visible.",
    ),
)


def get(act_id: str) -> Act:
    for act in ACTS:
        if act.id == act_id:
            return act
    raise KeyError(f"there is no act {act_id!r}; the acts are: {', '.join(a.id for a in ACTS)}")


def task_for(act: Act) -> bench_tasks.Task:
    return demo_tasks.TASK if act.task == demo_tasks.ID else bench_tasks.get(act.task)


def select(spec: str | None) -> list[Act]:
    """`--acts` text (`1,3`, `forge,attack`, `all`) to acts in demo order. An act that needs another one must come with it."""
    if not spec or spec.strip().lower() == "all":
        return list(ACTS)
    chosen: set[str] = set()
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        match = [a for a in ACTS if part == a.id or part == str(a.number)]
        if not match:
            raise ValueError(
                f"unknown act {part!r}; the acts are: {', '.join(f'{a.number} ({a.id})' for a in ACTS)}"
            )
        chosen.add(match[0].id)
    for act in ACTS:
        if act.id in chosen:
            missing = [n for n in act.needs if n not in chosen]
            if missing:
                raise ValueError(
                    f"act {act.number} ({act.id}) reuses what act {get(missing[0]).number} forges: run them together"
                )
    return [a for a in ACTS if a.id in chosen]
