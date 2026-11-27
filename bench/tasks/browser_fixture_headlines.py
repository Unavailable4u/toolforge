"""`browser_fixture_headlines` (tier 2): extract fields from a web page served by the fixture server.

A promoted story and a sponsored box look like stories and must be left out. The page is static HTML; P14b's browser plans or a plain
HTTP fetch and parse can both do it.
"""

from __future__ import annotations

import datetime as dt
import html
from collections.abc import Mapping
from typing import Any

from bench.tasks import FIXTURE_URL, Check, Instance, Task, answer_object, rng_for

ID = "browser_fixture_headlines"
PAGE_PATH = "/news/index.html"
SUBJECTS = (
    "Council",
    "Harbour",
    "Library",
    "Orchestra",
    "Rail line",
    "Bakery",
    "Museum",
    "Bridge",
    "School",
    "Market",
)
VERBS = ("approves", "reopens", "delays", "expands", "celebrates", "rethinks", "funds", "closes")
OBJECTS = ("new budget", "night service", "east wing", "summer plan", "100th year", "repairs", "late hours")
AUTHORS = ("Ana Ruiz", "Bram de Vries", "Chen Li", "Dara O'Neill", "Emeka Obi", "Freya Lund", "Gita Rao")

PROMPT = f"""\
A news front page is served at {FIXTURE_URL}{PAGE_PATH} (plain HTTP). Each story is an <article class="story"> with the headline
in its <h2>, the author in <span class="byline"> (written "By <name>") and the date in the datetime attribute of its <time>.
Leave out any article that also has the class "promo", and anything that is not an article with the class "story".

Answer with one JSON object of exactly this shape, stories in page order:
{{"headlines": [{{"title": "<headline>", "author": "<name, without 'By '>", "date": "YYYY-MM-DD"}}, ...]}}"""


def _story(number: int, title: str, author: str, day: dt.date, classes: str) -> str:
    return (
        f'<article class="{classes}">\n'
        f'  <h2><a href="/news/{number}">{html.escape(title)}</a></h2>\n'
        f'  <span class="byline">By {html.escape(author)}</span>\n'
        f'  <time datetime="{day.isoformat()}T08:00:00Z">{day.strftime("%d %B %Y")}</time>\n'
        f"</article>\n"
    )


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    base = dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 300))
    blocks = ["<!doctype html>\n<html><head><title>The Daily Fixture</title></head><body>\n<main>\n"]
    expected = []
    for index in range(rng.randint(5, 9)):
        title = f"{rng.choice(SUBJECTS)} {rng.choice(VERBS)} {rng.choice(OBJECTS)}"
        author = rng.choice(AUTHORS)
        day = base - dt.timedelta(days=rng.randint(0, 6))
        roll = rng.random()
        if roll < 0.15 and index:
            blocks.append(
                _story(100 + index, f"Sponsored: {rng.choice(SUBJECTS)} deals", "Partner", day, "story promo")
            )
        elif roll < 0.25:
            blocks.append(
                f'<aside class="teaser"><h2>{html.escape(rng.choice(SUBJECTS))} in pictures</h2></aside>\n'
            )
        blocks.append(_story(index, title, author, day, "story"))
        expected.append({"title": title, "author": author, "date": day.isoformat()})
    blocks.append("</main>\n</body></html>\n")
    page = "".join(blocks)
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT,
        files={},
        expected={"headlines": expected},
        served={PAGE_PATH: ("text/html; charset=utf-8", page.encode("utf-8"))},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    given = data.get("headlines") if data else None
    if not isinstance(given, list):
        return Check(False, "the answer has no 'headlines' list")
    expected = instance.expected["headlines"]
    if len(given) != len(expected):
        return Check(False, f"expected {len(expected)} stories, got {len(given)}")
    for index, (want, got) in enumerate(zip(expected, given, strict=True)):
        if not isinstance(got, dict):
            return Check(False, f"story {index + 1} is not an object")
        cleaned = {k: str(got.get(k, "")).strip() for k in want}
        if cleaned != want:
            return Check(False, f"story {index + 1}: expected {want}, got {cleaned}"[:300])
    return Check(True, "every story matches")


TASK = Task(
    id=ID,
    tier="2",
    title="Fields from a fixture web page",
    capability="load a web page and extract structured fields, ignoring look-alike blocks",
    generate=generate,
    check=check,
    has_sibling=False,
    needs=("fixture_site",),
)
