"""What the registry remembers about how tools fail and get repaired: failure captures, problem reports and repair attempts (P12).

Three tables, one module, all of it plain storage. Nothing here decides anything about a tool's status: that is `lifecycle` (and, for a
tool that keeps failing, `services/failures.py`, which calls `Registry.transition`).

* **Failure captures** (`regression_cases`). One row per call that ended in a failure of the tool. The row always holds the error class and
  a shape fingerprint (types and sizes, never values). It holds the raw arguments (`input_json`) only when the caller of `record_failure`
  passes them, which `services/failures.py` does only when `store_failed_inputs` is on and the input is small and has no secret-shaped
  text. At most `RAW_INPUTS_PER_TOOL` raw inputs are kept for a tool: when a newer one arrives the oldest is blanked (its fingerprint
  row stays), so turning the setting on does not make the registry a growing archive of what callers sent. The same input twice is kept
  once.
* **Problem reports.** What an agent says about a version. Capped per version and rate limited per reporter. A report is data for the
  operator; it never changes a status (`tests/test_problem_reports.py`).
* **Repairs.** One row per `forge_repair` attempt on a base version. They are what the per-hour limit counts, and a base version whose
  repairs failed `REPAIR_FLAG_AFTER` times in a row is flagged for the operator: repair refuses it from then on and `forge_status` says
  so. The flag is derived from the rows, so it disappears when another version becomes the base.

`recent_outcomes` looks only at calls made since the version last changed status. A released or rolled-back version therefore starts
with a clean window; without that, the failures that got it quarantined would quarantine it again on its next call.

Proven by `tests/test_registry_health.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from toolforge.errors import RegistryError
from toolforge.registry.db import Database

RAW_INPUTS_PER_TOOL = 20
MAX_REPORTS_PER_VERSION = 20
REPORTS_PER_HOUR = 5  # per reporter, across all tools
MAX_REPORT_CHARS = 500
MAX_REPORTER_CHARS = 80
REPAIR_FLAG_AFTER = 3
HOUR_S = 3600

# How a repair attempt ended. `started` is a row whose repair has not finished (or whose process died).
REPAIR_OUTCOMES = ("started", "verified", "failed", "aborted")


def _ago(db: Database, seconds: int) -> str:
    """The registry's own clock minus `seconds`, in the registry's timestamp format (so it compares as text)."""
    now = db.now()
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return (moment - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --- Failure captures --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """One captured failure. `args` is None unless the raw input was stored."""

    id: int
    version: str
    error_class: str
    shape: dict[str, Any]
    args: dict[str, Any] | None
    created_at: str


def record_failure(
    db: Database,
    version_id: int,
    error_class: str,
    shape: dict[str, Any],
    *,
    input_json: str | None = None,
    input_sha256: str = "",
) -> int:
    """Store one failure and return its row id. `input_json` is the raw argument object as JSON, or None to keep only the shape."""
    try:
        shape_text = json.dumps(shape, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise RegistryError("the failure shape is not plain JSON", code="invalid_value") from None
    with db.transaction() as conn:
        row = conn.execute("SELECT name FROM versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise RegistryError(f"no version with id {version_id}", code="unknown_version")
        name = row["name"]
        keep = input_json
        if (
            keep is not None
            and conn.execute(
                "SELECT 1 FROM regression_cases WHERE name = ? AND input_sha256 = ? AND input_json IS NOT NULL",
                (name, input_sha256),
            ).fetchone()
        ):
            keep = None  # this very input is already a case
        cursor = conn.execute(
            "INSERT INTO regression_cases (name, version_id, error_class, shape_json, input_json, input_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, version_id, error_class[:80], shape_text, keep, input_sha256[:64], db.now()),
        )
        conn.execute(
            "UPDATE regression_cases SET input_json = NULL WHERE name = ? AND input_json IS NOT NULL AND id NOT IN "
            "(SELECT id FROM regression_cases WHERE name = ? AND input_json IS NOT NULL ORDER BY id DESC LIMIT ?)",
            (name, name, RAW_INPUTS_PER_TOOL),
        )
        return int(cursor.lastrowid or 0)


def _failure(row: Any) -> Failure:
    args = json.loads(row["input_json"]) if row["input_json"] is not None else None
    return Failure(
        row["id"],
        row["version"],
        row["error_class"],
        json.loads(row["shape_json"]),
        args if isinstance(args, dict) else None,
        row["created_at"],
    )


def failures(db: Database, name: str, *, limit: int = 50) -> list[Failure]:
    """The newest captured failures of a tool (any of its versions), newest first."""
    rows = db.query(
        "SELECT r.*, v.version AS version FROM regression_cases r JOIN versions v ON v.id = r.version_id "
        "WHERE r.name = ? ORDER BY r.id DESC LIMIT ?",
        (name, max(1, min(int(limit), 500))),
    )
    return [_failure(row) for row in rows]


def regression_inputs(db: Database, name: str, *, limit: int = RAW_INPUTS_PER_TOOL) -> list[Failure]:
    """The stored raw inputs of a tool's failures: the regression cases a new version must survive. Newest first."""
    rows = db.query(
        "SELECT r.*, v.version AS version FROM regression_cases r JOIN versions v ON v.id = r.version_id "
        "WHERE r.name = ? AND r.input_json IS NOT NULL ORDER BY r.id DESC LIMIT ?",
        (name, max(1, min(int(limit), RAW_INPUTS_PER_TOOL))),
    )
    return [_failure(row) for row in rows]


def recent_outcomes(db: Database, version_id: int, count: int) -> list[tuple[bool, str | None]]:
    """`(ok, error_class)` of the last `count` calls a version made since it last changed status, newest first.

    A version that was quarantined and released (or deprecated and rolled back) starts again with an empty window: its old failures
    are the reason it was pulled, and counting them again would pull it on its first call back.
    """
    rows = db.query(
        "SELECT c.ok, c.error_class FROM calls c JOIN versions v ON v.id = c.version_id "
        "WHERE c.version_id = ? AND c.ts >= v.updated_at ORDER BY c.id DESC LIMIT ?",
        (version_id, count),
    )
    return [(bool(row["ok"]), row["error_class"]) for row in rows]


# --- Problem reports ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProblemReport:
    id: int
    name: str
    version: str
    reporter: str
    description: str
    created_at: str


def add_problem_report(db: Database, version_id: int, reporter: str, description: str) -> int:
    """Store a report. `description` must already be cleaned by the caller. Refused (`report_rate_limited`, `report_cap`) when the
    reporter already sent `REPORTS_PER_HOUR` in the last hour or the version already holds `MAX_REPORTS_PER_VERSION`."""
    who = (reporter or "agent").strip()[:MAX_REPORTER_CHARS] or "agent"
    text = description.strip()[:MAX_REPORT_CHARS]
    if not text:
        raise RegistryError("a problem report needs a description", code="invalid_value")
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM versions WHERE id = ?", (version_id,)).fetchone() is None:
            raise RegistryError(f"no version with id {version_id}", code="unknown_version")
        recent = conn.execute(
            "SELECT COUNT(*) AS n FROM problem_reports WHERE reporter = ? AND created_at >= ?",
            (who, _ago(db, HOUR_S)),
        ).fetchone()["n"]
        if recent >= REPORTS_PER_HOUR:
            raise RegistryError(
                f"at most {REPORTS_PER_HOUR} problem reports an hour are accepted from one reporter",
                code="report_rate_limited",
                hint="the operator already has your earlier reports; wait before sending more",
            )
        held = conn.execute(
            "SELECT COUNT(*) AS n FROM problem_reports WHERE version_id = ?", (version_id,)
        ).fetchone()["n"]
        if held >= MAX_REPORTS_PER_VERSION:
            raise RegistryError(
                f"this version already has {MAX_REPORTS_PER_VERSION} problem reports, which is as many as are kept",
                code="report_cap",
                hint="the operator has plenty to read; a new report adds nothing until they have dealt with these",
            )
        cursor = conn.execute(
            "INSERT INTO problem_reports (version_id, reporter, description, created_at) VALUES (?, ?, ?, ?)",
            (version_id, who, text, db.now()),
        )
        return int(cursor.lastrowid or 0)


def problem_reports(
    db: Database, *, version_id: int | None = None, name: str | None = None, limit: int = 50
) -> list[ProblemReport]:
    """Reports, newest first; for one version, for every version of a tool, or all of them."""
    clauses, params = [], []
    if version_id is not None:
        clauses.append("p.version_id = ?")
        params.append(version_id)
    if name is not None:
        clauses.append("v.name = ?")
        params.append(name)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query(
        "SELECT p.*, v.name AS name, v.version AS version FROM problem_reports p JOIN versions v ON v.id = p.version_id"
        f"{where} ORDER BY p.id DESC LIMIT ?",
        (*params, max(1, min(int(limit), 500))),
    )
    return [
        ProblemReport(r["id"], r["name"], r["version"], r["reporter"], r["description"], r["created_at"])
        for r in rows
    ]


def count_problem_reports(db: Database, version_id: int) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM problem_reports WHERE version_id = ?", (version_id,))
    return int(row["n"]) if row else 0


# --- Repairs -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairState:
    """Where a tool's repairs stand for one base version."""

    attempts_last_hour: int
    failed_in_a_row: int  # the most recent attempts on this base that failed (counting stops at the first that ended verified)
    flagged: bool  # `failed_in_a_row >= REPAIR_FLAG_AFTER`: the operator has to look


def repair_state(db: Database, name: str, base_version: str) -> RepairState:
    hour = db.query_one(
        "SELECT COUNT(*) AS n FROM repairs WHERE name = ? AND created_at >= ?", (name, _ago(db, HOUR_S))
    )
    rows = db.query(
        "SELECT outcome FROM repairs WHERE name = ? AND base_version = ? AND outcome != 'started' ORDER BY id DESC",
        (name, base_version),
    )
    streak = 0
    for row in rows:
        if row["outcome"] == "verified":
            break
        if (
            row["outcome"] == "failed"
        ):  # an aborted attempt (the sandbox was down) says nothing about the repair
            streak += 1
    return RepairState(int(hour["n"]) if hour else 0, streak, streak >= REPAIR_FLAG_AFTER)


def begin_repair(db: Database, name: str, base_version: str, *, max_per_hour: int) -> int:
    """Count a repair attempt, or refuse it: `repair_rate_limited` over the per-hour limit, `repair_flagged` for a base version
    whose repairs keep failing. The check and the insert are one transaction, so two callers cannot both take the last place."""
    with db.transaction() as conn:
        state = repair_state(db, name, base_version)
        if state.flagged:
            raise RegistryError(
                f"{name}@{base_version} was repaired {state.failed_in_a_row} times without a version that passed, so it is flagged for the operator",
                code="repair_flagged",
                hint="the operator decides: read `toolforge problems`, then promote a hand-made fix, roll back or release",
            )
        if state.attempts_last_hour >= max_per_hour:
            raise RegistryError(
                f"{name} was already repaired {state.attempts_last_hour} times in the last hour; the limit is {max_per_hour}",
                code="repair_rate_limited",
                hint="wait, or let the operator look at it (`toolforge problems`)",
            )
        cursor = conn.execute(
            "INSERT INTO repairs (name, base_version, outcome, created_at) VALUES (?, ?, 'started', ?)",
            (name, base_version, db.now()),
        )
        return int(cursor.lastrowid or 0)


def finish_repair(db: Database, repair_id: int, outcome: str, child_version: str | None = None) -> None:
    if outcome not in REPAIR_OUTCOMES[1:]:
        raise RegistryError(f"a repair ends as one of {', '.join(REPAIR_OUTCOMES[1:])}", code="invalid_value")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE repairs SET outcome = ?, child_version = ?, finished_at = ? WHERE id = ? AND outcome = 'started'",
            (outcome, child_version, db.now(), repair_id),
        )


@dataclass(frozen=True)
class Repair:
    id: int
    name: str
    base_version: str
    outcome: str
    child_version: str | None
    created_at: str


def repairs(db: Database, name: str, *, limit: int = 10) -> list[Repair]:
    """The newest repair attempts of a tool, newest first."""
    rows = db.query(
        "SELECT id, name, base_version, outcome, child_version, created_at FROM repairs WHERE name = ? "
        "ORDER BY id DESC LIMIT ?",
        (name, max(1, min(int(limit), 200))),
    )
    return [
        Repair(r["id"], r["name"], r["base_version"], r["outcome"], r["child_version"], r["created_at"])
        for r in rows
    ]


__all__ = [
    "MAX_REPORTS_PER_VERSION",
    "RAW_INPUTS_PER_TOOL",
    "REPAIR_FLAG_AFTER",
    "REPAIR_OUTCOMES",
    "REPORTS_PER_HOUR",
    "Failure",
    "ProblemReport",
    "Repair",
    "RepairState",
    "add_problem_report",
    "begin_repair",
    "count_problem_reports",
    "failures",
    "finish_repair",
    "problem_reports",
    "recent_outcomes",
    "record_failure",
    "regression_inputs",
    "repair_state",
    "repairs",
]
