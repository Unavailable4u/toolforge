"""`log_stats` (tier 1): parse a log format that exists only in a written spec, and compute per-service statistics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bench.tasks import Check, Instance, Task, answer_object, is_int, rng_for

ID = "log_stats"
LEVELS = ("INFO", "WARN", "ERROR", "DEBUG")
SERVICE_POOLS = (
    ("auth", "billing", "search", "upload", "notify"),
    ("gateway", "ledger", "catalog", "mailer", "thumbs"),
)

SPEC = """\
Each record in service.log is one line:

    <epoch milliseconds>|<level, padded with spaces to 5 characters>|<key=value pairs separated by ';'>

Keys: svc (service name), lat (latency, with a unit suffix: 'us' microseconds or 'ms' milliseconds), st (HTTP status code).
Lines starting with '##' are comments. Lines starting with '>>' continue the previous record's message and carry no data.
Blank lines may appear. Keys may come in any order, and a record may carry extra keys, which you ignore."""

PROMPT = """\
You are given the file service.log (in inputs/). Its format is described here:

{spec}

For every service that appears in the log, report: the number of records, the number of records whose status is 500 or higher,
and the largest latency in whole microseconds.

Answer with one JSON object of exactly this shape:
{{"services": {{"<service>": {{"count": <int>, "errors": <int>, "max_lat_us": <int>}}, ...}}}}"""


def _record(rng, now: int, services: tuple[str, ...]) -> tuple[str, str, int, int]:
    svc = rng.choice(services)
    lat_us = rng.randint(80, 250_000)
    status = rng.choice((200, 200, 200, 201, 204, 301, 404, 429, 500, 502, 503))
    if rng.random() < 0.5 and lat_us >= 1000:
        lat_us -= lat_us % 1000  # whole milliseconds, written with the 'ms' unit
        lat = f"{lat_us // 1000}ms"
    else:
        lat = f"{lat_us}us"
    pairs = [f"svc={svc}", f"lat={lat}", f"st={status}"]
    if rng.random() < 0.3:
        pairs.append(f"trace={rng.getrandbits(40):010x}")
    rng.shuffle(pairs)
    level = rng.choice(LEVELS)
    return f"{now}|{level:<5}|{';'.join(pairs)}", svc, lat_us, status


def generate(seed: int, sibling: bool = False) -> Instance:
    rng = rng_for(ID, seed, sibling)
    services = SERVICE_POOLS[1 if sibling else 0][: rng.randint(3, 5)]
    now = 1_700_000_000_000 + rng.randint(0, 10**9)
    lines = [f"## export of {rng.choice(('node-a', 'node-b', 'node-c'))}", ""]
    stats: dict[str, dict[str, int]] = {}
    for _ in range(rng.randint(60, 120)):
        now += rng.randint(1, 5000)
        line, svc, lat_us, status = _record(rng, now, services)
        lines.append(line)
        entry = stats.setdefault(svc, {"count": 0, "errors": 0, "max_lat_us": 0})
        entry["count"] += 1
        entry["errors"] += status >= 500
        entry["max_lat_us"] = max(entry["max_lat_us"], lat_us)
        roll = rng.random()
        if roll < 0.08:
            lines.append(f">> retry st=599 lat=999999ms svc={rng.choice(services)}")
        elif roll < 0.12:
            lines.append("## rotated")
        elif roll < 0.15:
            lines.append("")
    text = "\n".join(lines) + "\n"
    return Instance(
        task_id=ID,
        seed=seed,
        sibling=sibling,
        prompt=PROMPT.format(spec=SPEC),
        files={"service.log": text.encode("utf-8")},
        expected={"services": stats},
    )


def check(instance: Instance, answer: Any, files: Mapping[str, bytes]) -> Check:
    data = answer_object(answer)
    if data is None or not isinstance(data.get("services"), dict):
        return Check(False, "the answer is not an object with a 'services' object")
    expected = instance.expected["services"]
    given = data["services"]
    if set(given) != set(expected):
        return Check(False, f"services differ: expected {sorted(expected)}, got {sorted(map(str, given))}")
    for name, want in expected.items():
        got = given[name]
        if not isinstance(got, dict):
            return Check(False, f"{name}: not an object")
        for key, value in want.items():
            if not is_int(got.get(key)) or got[key] != value:
                return Check(False, f"{name}.{key}: expected {value}, got {got.get(key)!r}")
    return Check(True, "all services match")


TASK = Task(
    id=ID,
    tier="1",
    title="Statistics from an unusual log format",
    capability="parse a log format defined only by a written spec and aggregate per service",
    generate=generate,
    check=check,
    has_sibling=True,
)
