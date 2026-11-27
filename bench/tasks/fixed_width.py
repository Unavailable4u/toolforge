"""`fixed_width` (tier 1): read a fixed-width report with page breaks, repeated headers, separators and trailing minus signs."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, is_int, rng_for

ID = "fixed_width"
WORDS = ("bolt", "nut", "washer", "bracket", "hinge", "spring", "gasket", "rivet", "clamp", "shim")

LAYOUT = """\
Columns (1-based, inclusive): code 1-6, description 7-30, quantity 31-38, amount 39-50.
Quantity is right-aligned and may use ',' as a thousands separator. Amount has two decimals, may use ',' as a thousands separator,
and a negative amount is written with a trailing minus ('12.50-'). Each page starts with a form feed character on its own line,
followed by a title line, a header line and a line of dashes; these are not data. A line of '=' signs is a section break, not data.
Lines whose code is '*****' are subtotals printed by the report writer: they are not data either. Blank lines carry no data."""

PROMPT = """\
You are given the report stock.txt (in inputs/). Layout:

{layout}

Report the number of data lines, the sum of all quantities and the sum of all amounts.

Answer with one JSON object of exactly this shape: {{"rows": <int>, "total_qty": <int>, "total_amount": "<sum with two decimals>"}}"""


def _number(value: int) -> str:
    return f"{value:,}"


def _amount(cents: int) -> str:
    text = f"{abs(cents) // 100:,}.{abs(cents) % 100:02d}"
    return text + "-" if cents < 0 else text


def _page_header(rng, page: int) -> list[str]:
    return [
        "\f",
        f"STOCK MOVEMENT REPORT{'':>20}PAGE {page:>3}",
        f"{'CODE':<6}{'DESCRIPTION':<24}{'QTY':>8}{'AMOUNT':>12}",
        "-" * 50,
    ]


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    lines: list[str] = []
    page = 1
    lines += _page_header(rng, page)
    rows, qty_total, cents_total, on_page = 0, 0, 0, 0
    subtotal = 0
    prefix = "Q" if sibling else "P"
    for _ in range(rng.randint(30, 60)):
        code = f"{prefix}{rng.randint(0, 99999):05d}"
        description = f"{rng.choice(WORDS)} {rng.choice(('M4', 'M6', 'steel', 'brass', 'zinc', 'large'))}"[
            :24
        ]
        qty = rng.randint(0, 25_000)
        cents = rng.randint(-500_000, 9_000_000)
        lines.append(f"{code:<6}{description:<24}{_number(qty):>8}{_amount(cents):>12}")
        rows += 1
        qty_total += qty
        cents_total += cents
        subtotal += cents
        on_page += 1
        roll = rng.random()
        if roll < 0.06:
            lines.append("")
        elif roll < 0.1:
            lines.append(f"{'*****':<6}{'subtotal':<24}{'':>8}{_amount(subtotal):>12}")
            subtotal = 0
        elif roll < 0.13:
            lines.append("=" * 50)
        if on_page >= rng.randint(12, 18):
            page += 1
            on_page = 0
            lines += _page_header(rng, page)
    total = Decimal(cents_total) / 100
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(layout=LAYOUT),
        files={"stock.txt": ("\n".join(lines) + "\n").encode("utf-8")},
        expected={"rows": rows, "total_qty": qty_total, "total_amount": f"{total:.2f}"},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None:
        return Check(False, "the answer is not a JSON object")
    want = instance.expected
    for key in ("rows", "total_qty"):
        if not is_int(data.get(key)) or data[key] != want[key]:
            return Check(False, f"{key}: expected {want[key]}, got {data.get(key)!r}")
    raw = data.get("total_amount")
    try:
        given = Decimal(str(raw).replace(",", "")) if isinstance(raw, (str, int, float)) else None
    except InvalidOperation:
        given = None
    if given is None or given != Decimal(want["total_amount"]):
        return Check(False, f"total_amount: expected {want['total_amount']}, got {raw!r}")
    return Check(True, "rows and totals match")


TASK = Task(
    id=ID,
    tier="1",
    title="A fixed-width report with quirks",
    capability="parse fixed-width columns with page headers, subtotal lines and trailing minus signs",
    generate=generate,
    check=check,
    has_sibling=True,
)
