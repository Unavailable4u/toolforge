"""`messy_dates` (tier 1): normalise ambiguous date strings under stated rules, rejecting impossible dates."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, rng_for

ID = "messy_dates"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LONG_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

RULES = """\
1. 'YYYY-MM-DD' and 'YYYYMMDD' are year, month, day.
2. With slashes ('D/M/YYYY' or 'DD/MM/YYYY') the order is day, month, year.
3. With dots ('M.D.YYYY' or 'MM.DD.YYYY') the order is month, day, year.
4. Month names ('Mar 5 2024', '5 March 2024', 'March 5, 2024') are English, full or three-letter, in any letter case.
5. In the slash, dot and month-name forms the year may have two digits, 'YY', which means 20YY.
6. Leading and trailing spaces do not matter.
7. A string that names no real calendar date (for example 2023-02-29), or matches none of these forms, becomes null."""

PROMPT = """\
You are given dates.txt (in inputs/), one date string per line. Rules:

{rules}

Answer with one JSON object of exactly this shape, one entry per input line, in order:
{{"dates": ["YYYY-MM-DD" or null, ...]}}"""


def _render(rng, day: dt.date) -> str:
    y, m, d = day.year, day.month, day.day
    yy = y % 100 if 2000 <= y <= 2099 and rng.random() < 0.3 else None
    year = f"{yy:02d}" if yy is not None else str(y)
    form = rng.randrange(7)
    if form == 0:
        text = f"{y}-{m:02d}-{d:02d}"
    elif form == 1:
        text = f"{y}{m:02d}{d:02d}"
    elif form == 2:
        text = f"{d}/{m}/{year}" if rng.random() < 0.5 else f"{d:02d}/{m:02d}/{year}"
    elif form == 3:
        text = f"{m}.{d}.{year}" if rng.random() < 0.5 else f"{m:02d}.{d:02d}.{year}"
    elif form == 4:
        text = f"{MONTHS[m - 1]} {d} {year}"
    elif form == 5:
        text = f"{d} {LONG_MONTHS[m - 1]} {year}"
    else:
        text = f"{LONG_MONTHS[m - 1]} {d}, {year}"
    if form >= 4:
        text = rng.choice((text, text.upper(), text.lower()))
    if rng.random() < 0.15:
        text = f"  {text} "
    return text


def _impossible(rng) -> str:
    y = rng.choice((2021, 2022, 2023, 2025))
    return rng.choice(
        (
            f"{y}-02-29",
            f"31/04/{y}",
            f"02.30.{y}",
            f"{y}1301",
            f"Feb 30 {y}",
            "not a date",
            f"{y}-00-10",
        )
    )


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    start = dt.date(2019 if not sibling else 2001, 1, 1)
    lines: list[str] = []
    expected: list[str | None] = []
    for _ in range(rng.randint(25, 40)):
        if rng.random() < 0.15:
            lines.append(_impossible(rng))
            expected.append(None)
            continue
        day = start + dt.timedelta(days=rng.randint(0, 3650))
        if rng.random() < 0.3:
            day = day.replace(day=rng.randint(1, 12))  # day and month both 12 or below: the rules decide
        lines.append(_render(rng, day))
        expected.append(day.isoformat())
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(rules=RULES),
        files={"dates.txt": ("\n".join(lines) + "\n").encode("utf-8")},
        expected={"dates": expected},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None or not isinstance(data.get("dates"), list):
        return Check(False, "the answer is not an object with a 'dates' list")
    expected, given = instance.expected["dates"], data["dates"]
    if len(given) != len(expected):
        return Check(False, f"expected {len(expected)} entries, got {len(given)}")
    wrong = [i for i, (want, got) in enumerate(zip(expected, given, strict=True)) if want != got]
    if wrong:
        i = wrong[0]
        return Check(
            False, f"{len(wrong)} wrong; first at line {i + 1}: expected {expected[i]!r}, got {given[i]!r}"
        )
    return Check(True, "every date matches")


TASK = Task(
    id=ID,
    tier="1",
    title="Ambiguous dates under stated rules",
    capability="normalise mixed date formats with explicit ambiguity rules and reject impossible dates",
    generate=generate,
    check=check,
    has_sibling=True,
)
