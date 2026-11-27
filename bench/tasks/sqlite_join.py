"""`sqlite_join` (tier 1): answer a question that needs a three-table join over a SQLite file."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, is_int, rng_for

ID = "sqlite_join"
REGIONS = (("north", "south", "east", "west"), ("alpha", "beta", "gamma", "delta", "omega"))
SKUS = ("A-100", "A-200", "B-110", "C-300", "C-310", "D-400")

PROMPT = """\
You are given the SQLite database shop.db (in inputs/). Tables:

    customers(id INTEGER PRIMARY KEY, name TEXT, region TEXT)
    orders(id INTEGER PRIMARY KEY, customer_id INTEGER, placed_on TEXT)   -- placed_on is 'YYYY-MM-DD'
    order_items(order_id INTEGER, sku TEXT, qty INTEGER, unit_cents INTEGER)

For each region, compute the revenue in cents (sum of qty * unit_cents) of orders placed from {start} to {end}, both days included.
Leave out customers whose name starts with 'TEST' (upper case). Every region that has at least one customer must appear, with 0 when
it has no qualifying revenue.

Answer with one JSON object of exactly this shape: {{"revenue_cents": {{"<region>": <int>, ...}}}}"""


def _date(rng, year: int) -> str:
    return f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"


def _database(statements: list[tuple[str, tuple]]) -> bytes:
    """Build the file through sqlite3 itself, then read its bytes. `journal_mode=OFF` and fixed page settings keep it stable."""
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA page_size=4096")
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    regions = REGIONS[1 if sibling else 0]
    year = 2025 if not sibling else 2024
    quarter = rng.randint(1, 4)
    start = f"{year}-{3 * quarter - 2:02d}-01"
    end = f"{year}-{3 * quarter:02d}-{(31 if quarter in (1, 4) else 30):02d}"
    statements: list[tuple[str, tuple]] = [
        ("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, region TEXT)", ()),
        ("CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, placed_on TEXT)", ()),
        ("CREATE TABLE order_items(order_id INTEGER, sku TEXT, qty INTEGER, unit_cents INTEGER)", ()),
    ]
    customers: dict[int, tuple[str, str]] = {}
    used_regions = rng.sample(regions, k=len(regions) - 1)  # one region has no customer at all
    for cid in range(1, rng.randint(12, 20) + 1):
        name = rng.choice(("Ada", "Bo", "Cy", "Di", "Ed", "Flo", "Gus")) + f" {cid}"
        if rng.random() < 0.15:
            name = "TEST " + name
        elif rng.random() < 0.1:
            name = "test " + name  # lower case: not excluded
        region = rng.choice(used_regions)
        customers[cid] = (name, region)
        statements.append(("INSERT INTO customers VALUES (?, ?, ?)", (cid, name, region)))
    revenue = {region: 0 for region in sorted({r for _, r in customers.values()})}
    for oid in range(1, rng.randint(40, 70) + 1):
        cid = rng.choice(list(customers))
        placed = _date(rng, rng.choice((year - 1, year, year, year + 1)))
        if rng.random() < 0.1:
            placed = rng.choice((start, end))  # the boundaries are included
        statements.append(("INSERT INTO orders VALUES (?, ?, ?)", (oid, cid, placed)))
        for _ in range(rng.randint(1, 4)):
            qty, cents = rng.randint(1, 9), rng.randint(99, 9_999)
            statements.append(
                ("INSERT INTO order_items VALUES (?, ?, ?, ?)", (oid, rng.choice(SKUS), qty, cents))
            )
            name, region = customers[cid]
            if start <= placed <= end and not name.startswith("TEST"):
                revenue[region] += qty * cents
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(start=start, end=end),
        files={"shop.db": _database(statements)},
        expected={"revenue_cents": revenue},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None or not isinstance(data.get("revenue_cents"), dict):
        return Check(False, "the answer is not an object with a 'revenue_cents' object")
    expected, given = instance.expected["revenue_cents"], data["revenue_cents"]
    if set(given) != set(expected):
        return Check(False, f"regions differ: expected {sorted(expected)}, got {sorted(map(str, given))}")
    for region, value in expected.items():
        if not is_int(given[region]) or given[region] != value:
            return Check(False, f"{region}: expected {value}, got {given[region]!r}")
    return Check(True, "every region matches")


TASK = Task(
    id=ID,
    tier="1",
    title="A multi-table question over SQLite",
    capability="open a SQLite file and answer a question that joins three tables with filters",
    generate=generate,
    check=check,
    has_sibling=True,
)
