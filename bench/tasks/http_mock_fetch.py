"""`http_mock_fetch` (tier 1): page through a recorded JSON API on the fixture server and total what it returns.

The API is served by the harness's fixture server (`bench/harness.py::FixtureServer`) from the pages this generator writes. A tool that
reads it must declare the fixture host, and the scripted operator approves exactly that host and no other (`docs/BENCHMARK.md`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from bench.tasks import FIXTURE_URL, Check, Instance, Task, answer_object, is_int, rng_for

ID = "http_mock_fetch"
PAGE_PATH = "/api/v1/items"

PROMPT = f"""\
A recorded inventory API is served at {FIXTURE_URL}{PAGE_PATH} (plain HTTP). Each response is a JSON object
{{"items": [{{"sku": "<text>", "price_cents": <int>, "in_stock": <bool>}}, ...], "next": "<path of the next page>" or null}}.
The first page is {PAGE_PATH}; follow "next" (a path on the same server) until it is null.

Count the items that are in stock and add up their price_cents. Answer with one JSON object of exactly this shape:
{{{{"in_stock_items": <int>, "in_stock_value_cents": <int>}}}}"""


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    pages = rng.randint(3, 6)
    served: dict[str, tuple[str, bytes]] = {}
    count, value = 0, 0
    for page in range(1, pages + 1):
        items = []
        for _ in range(rng.randint(4, 12)):
            item = {
                "sku": f"{rng.choice('ABCDEFG')}-{rng.randint(100, 999)}",
                "price_cents": rng.randint(50, 50_000),
                "in_stock": rng.random() < 0.6,
            }
            items.append(item)
            if item["in_stock"]:
                count += 1
                value += item["price_cents"]
        next_path = f"{PAGE_PATH}?page={page + 1}" if page < pages else None
        body = json.dumps({"items": items, "next": next_path}).encode("utf-8")
        served[f"{PAGE_PATH}?page={page}"] = ("application/json", body)
        if page == 1:
            served[PAGE_PATH] = ("application/json", body)
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT,
        files={},
        expected={"in_stock_items": count, "in_stock_value_cents": value},
        served=served,
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None:
        return Check(False, "the answer is not a JSON object")
    for key, want in instance.expected.items():
        if not is_int(data.get(key)) or data[key] != want:
            return Check(False, f"{key}: expected {want}, got {data.get(key)!r}")
    return Check(True, "count and total match")


TASK = Task(
    id=ID,
    tier="1",
    title="A paged JSON API through an approved host",
    capability="call an HTTP API on one approved host, follow pagination and aggregate",
    generate=generate,
    check=check,
    has_sibling=False,
    needs=("fixture_site",),
)
