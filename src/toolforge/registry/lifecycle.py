"""The version lifecycle: which status may follow which, who may cause it, and what it changes.

`EDGES` is the one definition of the state machine. Enforcement, the event type each move returns, and the table in
`docs/registry.md` all come from it (`tests/test_registry_docs.py` and `tests/test_registry_lifecycle.py` keep the docs and
the diagram in `docs/guide/02-architecture.md` equal to it). Anything not listed raises `IllegalTransition`.

Security properties, proved in `tests/test_registry_lifecycle.py`:

* Fail closed on privilege. A move that grants a tool its place in front of agents without a human (`verified_basic -> active`)
  or returns a tool that was pulled (`quarantined -> active`, `deprecated -> active`) needs `actor="operator"` and a reason.
  A version that declares network hosts or secrets can never reach `active` except through approval: promotion refuses it.
* A tool name has at most one `active` version. Making a version active deprecates the previous one, updates `tools` and
  the search index, and does all of it in one transaction, so a failure part way leaves nothing changed.
* `deprecated` is reachable only from `active`, so every deprecated version was active once: rolling back to it only brings back
  something that already passed every gate and approval.

The registry does not write ledgers. `transition` returns the event for the caller to append.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, NamedTuple

from toolforge.errors import IllegalTransition, RegistryError
from toolforge.registry import search
from toolforge.registry.db import Database, VersionRecord, row_to_version

OPERATOR = "operator"
MAX_REASON_CHARS = 500
MAX_ACTOR_CHARS = 80

SUBMITTED, VERIFYING, REJECTED = "submitted", "verifying", "rejected"
VERIFIED_BASIC, VERIFIED, PENDING_APPROVAL = "verified_basic", "verified", "pending_approval"
ACTIVE, DEPRECATED, QUARANTINED = "active", "deprecated", "quarantined"

STATUSES = (
    SUBMITTED,
    VERIFYING,
    REJECTED,
    VERIFIED_BASIC,
    VERIFIED,
    PENDING_APPROVAL,
    ACTIVE,
    DEPRECATED,
    QUARANTINED,
)


class Edge(NamedTuple):
    event: str  # the ledger event type `transition` returns for this move
    note: str  # the rule in one line, as shown in the docs
    operator_only: bool = False  # the actor must be "operator"
    reason_required: bool = False  # a non-empty reason must be given
    refuse_network_or_secrets: bool = False  # refused when the manifest declares network hosts or secrets


EDGES: dict[tuple[str, str], Edge] = {
    (SUBMITTED, VERIFYING): Edge("forge.verifying", "The pipeline starts."),
    (VERIFYING, REJECTED): Edge("forge.rejected", "A gate failed."),
    (VERIFYING, VERIFIED_BASIC): Edge(
        "forge.verified", "Every gate that could run passed, but one was skipped."
    ),
    (VERIFYING, VERIFIED): Edge("forge.verified", "Gates G1 to G6 passed."),
    (VERIFIED_BASIC, ACTIVE): Edge(
        "forge.promoted",
        "Operator `promote` only, with a reason. Refused when the manifest declares network hosts or secrets.",
        operator_only=True,
        reason_required=True,
        refuse_network_or_secrets=True,
    ),
    (VERIFIED_BASIC, PENDING_APPROVAL): Edge(
        "forge.approval.requested",
        "The manifest asks for network or secrets and G5 was skipped; the request says so (G7).",
    ),
    (VERIFIED, PENDING_APPROVAL): Edge(
        "forge.approval.requested", "The manifest asks for network or secrets (G7)."
    ),
    (VERIFIED, ACTIVE): Edge(
        "forge.promoted",
        "Automatic promotion. Refused when the manifest declares network hosts or secrets.",
        refuse_network_or_secrets=True,
    ),
    (PENDING_APPROVAL, ACTIVE): Edge(
        "forge.approval.granted", "The operator approves; live examples run first.", operator_only=True
    ),
    (PENDING_APPROVAL, REJECTED): Edge(
        "forge.approval.denied",
        "The operator denies, the request times out, a live example fails, or a brokered secret has no broker service.",
    ),
    (ACTIVE, DEPRECATED): Edge(
        "forge.deprecated", "A newer version was promoted, or the operator retires it."
    ),
    (ACTIVE, QUARANTINED): Edge(
        "forge.quarantined", "A runtime violation or a failure rate over the threshold."
    ),
    (QUARANTINED, ACTIVE): Edge(
        "forge.released", "Operator `release`, with a reason (P08).", operator_only=True, reason_required=True
    ),
    (DEPRECATED, ACTIVE): Edge(
        "forge.rolled_back",
        "Operator `rollback`, with a reason (P12).",
        operator_only=True,
        reason_required=True,
    ),
}

TRANSITIONS: dict[str, tuple[str, ...]] = {
    status: tuple(to for (frm, to) in EDGES if frm == status) for status in STATUSES
}


def transitions_markdown() -> str:
    """The state machine as a Markdown table. `docs/registry.md` embeds exactly this text (drift-tested)."""
    rows = ["| From | To | Event | Rule |", "| --- | --- | --- | --- |"]
    for (frm, to), edge in EDGES.items():
        rows.append(f"| `{frm}` | `{to}` | `{edge.event}` | {edge.note} |")
    return "\n".join(rows)


# --- Checks --------------------------------------------------------------------------------------------------------------


def _clean_actor(actor: object) -> str:
    if not isinstance(actor, str) or not actor.strip() or not actor.isprintable():
        raise RegistryError(
            "a transition needs an actor: a short plain-text name such as 'pipeline' or 'operator'",
            code="invalid_actor",
        )
    return actor.strip()[:MAX_ACTOR_CHARS]


def _clean_reason(reason: object) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise RegistryError("the reason must be text", code="invalid_reason")
    text = "".join(char if char.isprintable() else " " for char in reason).strip()
    return text[:MAX_REASON_CHARS] or None


def _check_edge(record: VersionRecord, to: str, edge: Edge, actor: str, reason: str | None) -> None:
    if edge.operator_only and actor != OPERATOR:
        raise IllegalTransition(
            f"{record.label} cannot move from {record.status} to {to} for actor {actor!r}: only the operator may do this",
            hint="use the operator command line (promote, release, rollback, approvals); no agent tool can",
        )
    if edge.reason_required and reason is None:
        raise IllegalTransition(
            f"moving {record.label} from {record.status} to {to} needs a reason",
            hint="give a short reason; it is written to the history",
        )
    if edge.refuse_network_or_secrets and record.asks_network_or_secrets:
        raise IllegalTransition(
            f"{record.label} declares network hosts or secrets, so it cannot become active without approval",
            hint="request approval so the operator can read the permission diff (status pending_approval)",
        )


# --- The transition ------------------------------------------------------------------------------------------------------


def _event(
    record: VersionRecord, edge: Edge, to: str, actor: str, reason: str | None, now: str
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": edge.event,
        "tool": record.name,
        "version": record.version,
        "version_id": record.id,
        "from": record.status,
        "to": to,
        "actor": actor,
        "code_sha": record.code_sha,
        "ts": now,
        "related": [],
    }
    if reason is not None:
        event["reason"] = reason
    return event


def _displace_active(db: Database, record: VersionRecord, actor: str, now: str) -> list[dict[str, Any]]:
    """Deprecate every other active version of this tool. The `status` column is the truth; `tools` is updated to match."""
    events: list[dict[str, Any]] = []
    rows = db.query(
        "SELECT * FROM versions WHERE name = ? AND status = ? AND id != ? ORDER BY id",
        (record.name, ACTIVE, record.id),
    )
    with db.transaction() as conn:
        for row in rows:
            previous = row_to_version(row)
            conn.execute(
                "UPDATE versions SET status = ?, updated_at = ? WHERE id = ?", (DEPRECATED, now, previous.id)
            )
            search.remove_version(db, previous.id)
            events.append(
                _event(
                    previous,
                    EDGES[(ACTIVE, DEPRECATED)],
                    DEPRECATED,
                    actor,
                    f"superseded by {record.version}",
                    now,
                )
            )
        conn.execute("UPDATE tools SET active_version = ? WHERE name = ?", (record.version, record.name))
    return events


def transition(
    db: Database, version_id: int, to: str, *, actor: str, reason: str | None = None
) -> dict[str, Any]:
    """Move a version to status `to` and return the ledger event for the caller to append.

    Raises `IllegalTransition` for a move the state machine does not allow, for a missing reason, for a non-operator actor on an
    operator-only move, and for a promotion of a version that declares network hosts or secrets. Nothing changes when it raises.
    The returned event is `{"type": "forge.<verb>", "tool", "version", "version_id", "from", "to", "actor", "code_sha", "ts",
    "reason"?, "related": [...]}`. `related` holds the events for other versions this move changed (the previous active
    version that was deprecated); each belongs in that version's own ledger.
    """
    actor = _clean_actor(actor)
    reason = _clean_reason(reason)
    with db.transaction() as conn:
        row = db.query_one("SELECT * FROM versions WHERE id = ?", (version_id,))
        if row is None:
            raise RegistryError(
                f"no version with id {version_id}",
                code="unknown_version",
                hint="list versions with `toolforge registry list`",
            )
        record = row_to_version(row)
        edge = EDGES.get((record.status, to))
        if edge is None:
            allowed = ", ".join(TRANSITIONS.get(record.status, ())) or "none (this status is final)"
            raise IllegalTransition(
                f"cannot move {record.label} from {record.status} to {to!s}; from {record.status} it may only go to: {allowed}",
                hint="check the lifecycle in docs/registry.md",
            )
        _check_edge(record, to, edge, actor, reason)
        now = db.now()
        event = _event(record, edge, to, actor, reason, now)
        if to == ACTIVE:
            event["related"] = _displace_active(db, record, actor, now)
        elif record.status == ACTIVE:
            conn.execute(
                "UPDATE tools SET active_version = NULL WHERE name = ? AND active_version = ?",
                (record.name, record.version),
            )
            search.remove_version(db, record.id)
        conn.execute("UPDATE versions SET status = ?, updated_at = ? WHERE id = ?", (to, now, record.id))
        if to == ACTIVE:
            search.index_version(db, replace(record, status=ACTIVE, updated_at=now))
        return event
