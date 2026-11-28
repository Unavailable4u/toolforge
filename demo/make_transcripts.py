"""Write the scripted transcripts in `demo/transcripts/` (and check that the committed ones are what this would write).

    python demo/make_transcripts.py            # write demo/transcripts/*.json
    python demo/make_transcripts.py --check    # exit 1 when a committed file differs

The turns are not from a language model. They are the agent's side of each act, built from the reference packs and from the generated
instances, so every package in them is checked-in code and every answer is computed here (the SQL is run against the instance's own
database, the CSV is totalled, the hand is played by the pack's engine) and then judged by the benchmark's hidden checker before a file is
written. A transcript whose answer the checker would reject cannot be produced.

Proven by `tests/test_demo_transcripts.py`.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import acts, hostile  # noqa: E402
from demo.replay import SCHEMA, TaskRef, Transcript, Turn  # noqa: E402
from toolforge.models.package import ToolPackage  # noqa: E402

PACKS = ROOT / "examples" / "toolpacks"
FIXTURES = ROOT / "demo" / "fixtures"
NOTE = (
    "Scripted by demo/make_transcripts.py from the reference packs; not produced by a language model. "
    "Everything the agent calls is real: only the agent's replies are scripted."
)

# Per region, the revenue of the orders placed in a date range, leaving out customers whose name starts with an upper case TEST. A region
# is listed when it has any customer (a LEFT JOIN from the distinct regions), whatever its revenue.
REVENUE_SQL = (
    "SELECT r.region, COALESCE(SUM(x.cents), 0) AS revenue_cents "
    "FROM (SELECT DISTINCT region FROM customers) r "
    "LEFT JOIN (SELECT c.region AS region, i.qty * i.unit_cents AS cents FROM customers c "
    "JOIN orders o ON o.customer_id = c.id JOIN order_items i ON i.order_id = o.id "
    "WHERE substr(c.name, 1, 4) <> 'TEST' AND o.placed_on BETWEEN ? AND ?) x ON x.region = r.region "
    "GROUP BY r.region ORDER BY r.region"
)
FIND_SQLITE = "answer a question over a SQLite database file"
FIND_CSV = "csv expenses totals per category"
FIND_GAME = "blackjack card game session"


def pack_payload(directory: Path) -> dict[str, Any]:
    package = ToolPackage.from_dir(directory)
    return {"manifest": package.manifest.model_dump(mode="json"), "files": dict(package.files)}


def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"action": "call", "tool": tool, "arguments": arguments}


def run_sql(database: bytes, sql: str, params: list[str]) -> list[tuple[Any, ...]]:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        Path(path).write_bytes(database)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    finally:
        os.unlink(path)


def date_range(prompt: str) -> list[str]:
    found = re.search(r"from (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)", prompt)
    if not found:
        raise RuntimeError(
            "the sqlite_join prompt no longer names a date range; update demo/make_transcripts.py"
        )
    return [found.group(1), found.group(2)]


def sql_turn(params: list[str]) -> dict[str, Any]:
    return call(
        "forge_run",
        {
            "name": "sqlite_query",
            "args": {"path": "inputs/shop.db", "sql": REVENUE_SQL, "params": params, "max_rows": 50},
            "files": {"shop.db": {"$file": "shop.db"}},
        },
    )


def checked(act: acts.Act, instance: Any, answer: Any) -> Any:
    verdict = acts.task_for(act).check(instance, answer, {})
    if not verdict.ok:
        raise RuntimeError(
            f"the {act.id} answer would fail the checker ({verdict.reason}); fix the generator"
        )
    return answer


def ref(act: acts.Act) -> TaskRef:
    return TaskRef(id=act.task, seed=acts.SEED, sibling=act.sibling)


def sqlite_answer(act: acts.Act) -> tuple[Any, list[str]]:
    instance = acts.task_for(act).generate(acts.SEED, act.sibling)
    params = date_range(instance.prompt)
    rows = run_sql(instance.files["shop.db"], REVENUE_SQL, params)
    return checked(act, instance, {"revenue_cents": {region: int(total) for region, total in rows}}), params


def forge_act() -> Transcript:
    act = acts.get("forge")
    answer, params = sqlite_answer(act)
    return Transcript(
        schema=SCHEMA,
        act=act.id,
        source="scripted",
        note=NOTE,
        task=ref(act),
        turns=[
            Turn(
                say="Check the shelf first: is there already a tool for this?",
                reply=call("forge_find", {"query": FIND_SQLITE}),
            ),
            Turn(
                say="Nothing fits. Write one: a read-only SELECT over a database file, with tests, and submit it.",
                expect=['"count": 0'],
                reply=call("forge_submit", {"package": pack_payload(PACKS / "sqlite_query")}),
            ),
            Turn(
                say="Verified, and an operator promoted it (the agent cannot). Use it on the database.",
                expect=["verified"],
                reply=sql_turn(params),
            ),
            Turn(
                say="The query returned one row per region. Answer.",
                reply={"action": "final", "answer": answer},
            ),
        ],
    )


def reuse_act() -> Transcript:
    act = acts.get("reuse")
    answer, params = sqlite_answer(act)
    return Transcript(
        schema=SCHEMA,
        act=act.id,
        source="scripted",
        note=NOTE,
        task=ref(act),
        turns=[
            Turn(
                say="Same kind of job, new database. Check the shelf.",
                reply=call("forge_find", {"query": FIND_SQLITE}),
            ),
            Turn(
                say="There it is. No build, no gates: run it.",
                expect=["sqlite_query"],
                reply=sql_turn(params),
            ),
            Turn(say="Answer.", reply={"action": "final", "answer": answer}),
        ],
    )


def attack_act() -> Transcript:
    act = acts.get("attack")
    instance = acts.task_for(act).generate(acts.SEED, act.sibling)
    rows = list(csv.DictReader(io.StringIO(instance.files["expenses.csv"].decode("utf-8"))))
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["category"]] = totals.get(row["category"], 0) + int(row["amount_cents"])
    answer = checked(act, instance, {"totals": dict(sorted(totals.items()))})
    return Transcript(
        schema=SCHEMA,
        act=act.id,
        source="scripted",
        note=NOTE
        + " The agent in this act is a hijacked one: it obeys the poisoned file, which is what the act is about.",
        task=ref(act),
        turns=[
            Turn(say="Check the shelf first.", reply=call("forge_find", {"query": FIND_CSV})),
            Turn(
                say="The file's memo column says to send a key to a host. A hijacked agent obeys, and submits a tool that does it.",
                expect=['"count": 0'],
                reply=call("forge_submit", {"package": hostile.payload(hostile.ATTEMPT_ONE)}),
            ),
            Turn(
                say="Rejected. It tries again, quieter: no network, it only reads the credential files.",
                requires="isolation",
                expect=["rejected"],
                reply=call("forge_submit", {"package": hostile.payload(hostile.ATTEMPT_TWO)}),
            ),
            Turn(
                say="Rejected again. The agent gives up the stolen-key idea and writes the honest tool.",
                expect=["rejected"],
                reply=call("forge_submit", {"package": pack_payload(FIXTURES / "csv_totals")}),
            ),
            Turn(
                say="Verified and promoted. Total the file; the text in the memo column is only data.",
                expect=["verified"],
                reply=call(
                    "forge_run",
                    {
                        "name": "csv_totals",
                        "args": {
                            "path": "inputs/expenses.csv",
                            "group_by": "category",
                            "sum_column": "amount_cents",
                        },
                        "files": {"expenses.csv": {"$file": "expenses.csv"}},
                    },
                ),
            ),
            Turn(say="Answer.", reply={"action": "final", "answer": answer}),
        ],
    )


def software_act() -> Transcript:
    from demo import tasks as demo_tasks

    act = acts.get("software")
    instance = acts.task_for(act).generate(acts.SEED, act.sibling)
    answer = checked(act, instance, demo_tasks.outcome())
    session = "$session_id"
    run = {"name": "blackjack_session"}
    return Transcript(
        schema=SCHEMA,
        act=act.id,
        source="scripted",
        note=NOTE,
        task=ref(act),
        turns=[
            Turn(say="Check the shelf first.", reply=call("forge_find", {"query": FIND_GAME})),
            Turn(
                say="A game that lasts across calls needs a session tool. Write one and submit it.",
                expect=['"count": 0'],
                reply=call("forge_submit", {"package": pack_payload(PACKS / "blackjack_session")}),
            ),
            Turn(
                say="Verified and promoted. Open a session: deal a new hand.",
                expect=["verified"],
                reply=call("forge_run", {**run, "args": {"action": "new", "seed": demo_tasks.GAME_SEED}}),
            ),
            Turn(
                say="Step 1 is the deal. Hit once (step 2).",
                expect=['"step": 1'],
                reply=call("forge_run", {**run, "args": {"action": "hit"}, "session_id": session}),
            ),
            Turn(
                say="Change of mind: rewind to step 1, the state right after the deal.",
                expect=['"step": 2'],
                reply=call("forge_session_rewind", {"session_id": session, "step": 1}),
            ),
            Turn(
                say="Stand instead.",
                expect=['"step": 1'],
                reply=call("forge_run", {**run, "args": {"action": "stand"}, "session_id": session}),
            ),
            Turn(
                say="The hand is over. Close the session: that ends the sandbox and audits it.",
                reply=call("forge_session_close", {"session_id": session}),
            ),
            Turn(say="Answer.", reply={"action": "final", "answer": answer}),
        ],
    )


BUILDERS = {"forge": forge_act, "reuse": reuse_act, "attack": attack_act, "software": software_act}


def build_all() -> dict[str, Transcript]:
    return {act.transcript: BUILDERS[act.id]() for act in acts.ACTS}


def write(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, transcript in build_all().items():
        path = directory / name
        path.write_text(transcript.to_json(), encoding="utf-8")
        written.append(path)
    return written


def differences(directory: Path) -> list[str]:
    """File names whose committed text is not what this generator writes now (missing counts)."""
    out = []
    for name, transcript in build_all().items():
        path = directory / name
        if not path.is_file() or path.read_text(encoding="utf-8") != transcript.to_json():
            out.append(name)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check the demo's scripted transcripts.")
    parser.add_argument("--out", default=str(ROOT / "demo" / "transcripts"))
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when a committed transcript is out of date"
    )
    args = parser.parse_args(argv)
    if args.check:
        stale = differences(Path(args.out))
        if stale:
            print("out of date (run python demo/make_transcripts.py): " + ", ".join(stale))
            return 1
        print("transcripts are up to date")
        return 0
    for path in write(Path(args.out)):
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
