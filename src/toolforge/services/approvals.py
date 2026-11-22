"""Approvals: what an operator reads before a tool may use the network or a secret, and how a request ends without a person.

A version whose manifest has non-empty `network` or `secrets` never becomes active on its own. After the pipeline has run it
becomes `pending_approval` and an **approval request** is opened: a row in the registry (the truth for its state) and a readable
copy under `$TOOLFORGE_HOME/approvals/<id>.json`. The request holds the permissions asked for, a **permission diff** against the
version that is active now, the tool's description, a one-line summary of every gate, the Blast Radius summary and the limits of
the egress proxy. Gates that did not run are named first, so the operator sees how much weaker the evidence is.

This module is everything about an approval except the decision. The decision (`decide`) lives in `operator_actions`, which the
MCP server never imports. What is here is safe for the server to call because it can only say no: an overdue request is denied
(`expire_overdue`), and a request for a brokered secret the operator never configured is denied at once (`open_request`).

Security properties, and the tests that prove them (`tests/test_approvals.py` unless named):
* A request is bound to the exact content: it records `code_sha`, and `decide` refuses when the version no longer has it
  (`tests/test_operator_actions.py`). Versions are immutable, so a changed package is a new version with a new request.
* No secret value is ever written: only the names of secrets and of brokered services, hosts, hashes and counts
  (`test_no_secret_value_reaches_the_request`).
* Text that came from the tool's author (its description) is cleaned and clipped before it is stored
  (`test_the_description_is_cleaned_and_clipped`).
* Fail closed. A missing broker service, a timeout and anything that cannot be read end in `rejected` with a reason, never in
  `active` (`test_a_secret_without_a_broker_service_is_rejected`, `test_an_overdue_request_is_denied`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from toolforge import ledger as ledger_mod
from toolforge import textsafety
from toolforge.config import Settings
from toolforge.errors import IllegalTransition, RegistryError
from toolforge.registry import Approval, Registry, VersionRecord, lifecycle

log = logging.getLogger(__name__)

APPROVALS_DIR = "approvals"
SCHEMA = 1
FORGE = "forge"  # the actor of a denial nobody typed: a timeout, a missing broker service
DESCRIPTION_CHARS = 400
SUMMARY_CHARS = 240
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # `registry.db.utc_now`

EGRESS_LIMITS = (
    "The egress proxy has one allowlist for every tool that runs on it, so while this tool runs it can reach any host that was "
    "approved for any tool, not only the hosts listed here.",
    "MiniLocker's proxy also lets through the subdomains of an allowed host (an approved example.org allows www.example.org).",
    "A brokered secret is reachable by every sandbox on the same proxy, because the proxy cannot tell sandboxes apart. The sandbox "
    "never holds the token; the token's own scope is the limit of what a misbehaving tool can do with it.",
)

BrokerLookup = Callable[[], Mapping[str, str]]  # {service name: host}; never a token
LedgerAppend = Callable[[str, dict[str, Any]], None]
LedgerFactory = Callable[[str | None, str], Any]


# --- What is being asked for -----------------------------------------------------------------------------------------------


def requested_permissions(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    """The network hosts and secret names a manifest asks for, sorted. Nothing else about a manifest is a sensitive permission."""
    perms = manifest.get("permissions") if isinstance(manifest.get("permissions"), Mapping) else {}
    return {
        "network": sorted({str(h) for h in (perms.get("network") or [])}),
        "secrets": sorted({str(n) for n in (perms.get("secrets") or [])}),
    }


def asks_for_permissions(manifest: Mapping[str, Any]) -> bool:
    asked = requested_permissions(manifest)
    return bool(asked["network"] or asked["secrets"])


def permission_diff(
    requested: Mapping[str, Sequence[str]],
    active: Mapping[str, Sequence[str]] | None,
    active_label: str | None,
) -> dict[str, Any]:
    """What this version adds to, keeps from and removes from the version that is active now.

    `active is None` means there is no active version of the tool: everything is new. A removed permission is shown too, because
    an operator who sees only additions cannot tell whether a tool narrowed or merely moved.
    """
    diff: dict[str, Any] = {"against": active_label, "no_active_version": active is None}
    for kind in ("network", "secrets"):
        new = set(requested.get(kind, ()))
        old = set(active.get(kind, ())) if active is not None else set()
        diff[kind] = {"added": sorted(new - old), "kept": sorted(new & old), "removed": sorted(old - new)}
    return diff


REQUIRED_GATES = (
    "G1",
    "G2",
    "G3",
    "G4",
)  # the gates that must have passed before anything is put in front of an operator


def require_gates(registry: Registry, record: VersionRecord) -> None:
    """Refuse a version whose latest G1 to G4 result is not `passed` (an approval never covers for a gate that did not pass)."""
    rows = registry.gate_results(record.id)
    for gate in REQUIRED_GATES:
        mine = [r for r in rows if r.gate == gate]
        if not mine or mine[-1].status != "passed":
            found = mine[-1].status if mine else "no result"
            raise IllegalTransition(
                f"{record.label} cannot be put up for approval: gate {gate} did not pass ({found})",
                code="gates_not_passed",
                hint="submit the package again so that verification can finish",
            )


def missing_brokers(secrets: Collection[str], configured: Collection[str]) -> list[str]:
    """The secret names for which the operator configured no broker service, sorted."""
    return sorted(set(secrets) - set(configured))


def _no_broker_reason(names: Sequence[str], problem: str | None = None) -> str:
    base = (
        f"no credential broker service is configured for: {', '.join(names)}. "
        "The operator sets MINILOCKER_BROKER=<name>=<host> and MINILOCKER_BROKER_TOKEN_<NAME> in the environment of the process that runs ToolForge, then the tool is submitted again as a new version"
    )
    return f"{base} ({problem})" if problem else base


def default_brokers(settings: Settings | None = None) -> tuple[dict[str, str], str | None]:
    """`({service: host}, problem)` from MiniLocker's reading of the environment. `problem` says why nothing could be read."""
    try:
        from toolforge.sandbox.minilocker_runner import broker_service_hosts

        return broker_service_hosts(settings), None
    except (
        Exception
    ) as exc:  # no MiniLocker, no Docker SDK: nothing can be brokered, and that is a result, not a crash
        return {}, f"the broker configuration could not be read: {type(exc).__name__}"


# --- Time ------------------------------------------------------------------------------------------------------------------


def parse_time(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _format_time(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)[:-4] + "Z"  # milliseconds, as `utc_now`


def _clip(text: object, limit: int) -> str:
    raw = text if isinstance(text, str) else str(text)
    clean = textsafety.sanitize(raw.encode("utf-8", "replace").decode("utf-8"), max_chars=max(limit, 40))
    return clean if len(clean) <= limit else clean[:limit]


# --- The request -----------------------------------------------------------------------------------------------------------


def _gate_rows(gates: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "id": _clip(g.get("id", "?"), 8),
            "name": _clip(g.get("name", ""), 40),
            "status": _clip(g.get("status", "?"), 12),
            "summary": _clip(g.get("summary", ""), SUMMARY_CHARS),
        }
        for g in gates
    ]


def build_request(
    record: VersionRecord,
    *,
    active: VersionRecord | None,
    gates: Sequence[Mapping[str, Any]],
    blast_radius: Mapping[str, Any],
    created_at: str,
    expires_at: str,
    brokers: Mapping[str, str],
    broker_problem: str | None = None,
) -> dict[str, Any]:
    """The request document. Plain JSON, no secret value, every untrusted string cleaned and clipped."""
    asked = requested_permissions(record.manifest)
    rows = _gate_rows(gates)
    description = _clip(record.manifest.get("description", ""), DESCRIPTION_CHARS)
    scan = textsafety.scan(description)
    live = [e for e in record.manifest.get("examples") or [] if isinstance(e, Mapping) and e.get("live")]
    return {
        "schema": SCHEMA,
        "tool": record.label,
        "name": record.name,
        "version": record.version,
        "code_sha": record.code_sha,
        "requested": asked,
        "permission_diff": permission_diff(
            asked,
            requested_permissions(active.manifest) if active is not None else None,
            active.label if active else None,
        ),
        "skipped_gates": [r["id"] for r in rows if r["status"] == "skipped"],
        "gates": rows,
        "description": description,
        "description_scan": {
            "measured": bool(scan.get("measured")),
            "suspected": scan.get("suspected"),
        },
        "blast_radius": _plain_json(blast_radius),
        "live_examples": len(live),
        "brokers": {
            name: {"configured": name in brokers, "host": brokers.get(name)} for name in asked["secrets"]
        },
        "broker_problem": _clip(broker_problem, 200) if broker_problem else None,
        "limits": list(EGRESS_LIMITS),
        "created_at": created_at,
        "expires_at": expires_at,
    }


def _plain_json(value: Any, depth: int = 0) -> Any:
    """`value` reduced to plain JSON, strings cleaned and clipped: the report summary came from a run of the tool."""
    if depth > 6:
        return None
    if isinstance(value, str):
        return _clip(value, 300)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, Mapping):
        return {_clip(k, 60): _plain_json(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_plain_json(v, depth + 1) for v in list(value)[:40]]
    return _clip(value, 100)


def request_path(settings: Settings, approval_id: int) -> Path:
    return Path(settings.home) / APPROVALS_DIR / f"{approval_id}.json"


def write_request_file(settings: Settings, approval_id: int, document: Mapping[str, Any]) -> Path:
    """Write the readable copy of a request. It never changes afterwards: the registry holds the state, this holds what was asked."""
    path = request_path(settings, approval_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = (
        json.dumps({"approval_id": approval_id, **document}, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n"
    )
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{approval_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return path


@dataclass(frozen=True)
class Requested:
    """What `open_request` did. `state` is `pending` (waiting for the operator) or `denied` (it ended at once, with `reason`)."""

    approval_id: int
    state: str
    reason: str | None
    file: str | None


def _stored_blast_radius(settings: Settings, label: str) -> dict[str, Any]:
    from toolforge.report import blast_radius

    try:
        document, problem = blast_radius.load_stored(settings, label)
        return blast_radius.summarize(document) if document else blast_radius.missing_summary(problem)
    except Exception as exc:  # a report that cannot be read is shown as missing: it must not stop a request from being opened
        return blast_radius.missing_summary(f"the stored report could not be read ({type(exc).__name__})")


def open_request(
    settings: Settings,
    registry: Registry,
    record: VersionRecord,
    gates: Sequence[Mapping[str, Any]],
    *,
    ledger_append: LedgerAppend | None = None,
    brokers: BrokerLookup | None = None,
) -> Requested:
    """Move a verified version to `pending_approval` and open its request. Returns what happened.

    `record` must be `verified` or `verified_basic` and must ask for network hosts or secrets. `gates` is one dict per gate that was
    reported (`id`, `name`, `status`, `summary`). `ledger_append(type, payload)` receives `forge.approval.requested` and, when the
    request ends at once, `forge.approval.denied`. `brokers` says which brokered services exist (`{name: host}`); the default asks
    MiniLocker. A request for a secret with no broker service is opened and denied in the same step, so the history shows the
    request and why it ended, and the version is `rejected`.
    """
    if not asks_for_permissions(record.manifest):
        raise IllegalTransition(
            f"{record.label} asks for no network host and no secret, so it needs no approval",
            code="no_approval_needed",
            hint="promote it with `toolforge promote`, or leave it to automatic promotion",
        )
    require_gates(registry, record)
    asked = requested_permissions(record.manifest)
    known: dict[str, str] = {}
    problem: str | None = None
    if asked["secrets"]:  # only a tool that asks for a secret depends on the broker configuration
        if brokers is not None:
            known = dict(brokers())
        else:
            known, problem = default_brokers(settings)
    absent = missing_brokers(asked["secrets"], known)
    now = registry.db.now()
    start = parse_time(now) or datetime.now(UTC)
    expires = _format_time(start + timedelta(seconds=settings.approval_timeout_s))
    document = build_request(
        record,
        active=registry.active_version(record.name),
        gates=gates,
        blast_radius=_stored_blast_radius(settings, record.label),
        created_at=now,
        expires_at=expires,
        brokers=known,
        broker_problem=problem if absent else None,
    )
    with registry.db.transaction():
        event = registry.transition(record.id, lifecycle.PENDING_APPROVAL, actor=FORGE)
        approval = registry.add_approval(record.id, document)
    path: str | None = None
    try:
        path = str(write_request_file(settings, approval.id, document))
    except OSError:
        log.warning(
            "the approval request %s of %s could not be written to disk",
            approval.id,
            record.label,
            exc_info=True,
        )
    _append(
        ledger_append,
        event,
        {
            "approval_id": approval.id,
            "requested": asked,
            "skipped_gates": document["skipped_gates"],
            "expires_at": expires,
        },
    )
    if absent:
        reason = _no_broker_reason(absent, problem)
        denied = deny(registry, approval.id, record.id, by=FORGE, reason=reason)
        _append(ledger_append, denied, {"approval_id": approval.id})
        return Requested(approval.id, "denied", reason, path)
    return Requested(approval.id, "pending", None, path)


def _append(ledger_append: LedgerAppend | None, event: Mapping[str, Any], extra: Mapping[str, Any]) -> None:
    if ledger_append is None:
        return
    payload = {key: value for key, value in event.items() if key not in ("type", "related")}
    payload.update(extra)
    ledger_append(str(event["type"]), payload)


# --- Ending a request without a person ---------------------------------------------------------------------------------------


def deny(registry: Registry, approval_id: int, version_id: int, *, by: str, reason: str) -> dict[str, Any]:
    """Record a denial and reject the version, in one transaction. Returns the `forge.approval.denied` event for the caller to append.

    Used for the automatic endings and, through `operator_actions.decide`, for the operator's own denial. A request that was decided
    already, or a version that is no longer `pending_approval`, raises (nothing changes).
    """
    with registry.db.transaction():
        registry.decide_approval(approval_id, "denied", decided_by=by)
        return registry.transition(version_id, lifecycle.REJECTED, actor=by, reason=reason)


@dataclass(frozen=True)
class Expired:
    approval_id: int
    label: str


def expire_overdue(
    settings: Settings,
    registry: Registry,
    *,
    ledger_factory: LedgerFactory | None = None,
    version_id: int | None = None,
) -> list[Expired]:
    """Deny every pending request whose `expires_at` has passed (only those of `version_id` when given). Returns what was denied.

    Nobody runs a timer, so this runs whenever somebody looks: the `approvals` commands, `forge_status`, and `decide`. It can only
    say no, so it is safe for the server to call. A request whose document has no readable `expires_at` is left pending: an
    approval is never granted or denied on a guess.
    """
    now = parse_time(registry.db.now())
    if now is None:
        return []
    done: list[Expired] = []
    for approval in registry.list_approvals("pending"):
        if version_id is not None and approval.version_id != version_id:
            continue
        expires = parse_time((approval.requested or {}).get("expires_at"))
        if expires is None or now < expires:
            continue
        record = registry.get_version(approval.version_id)
        if record is None:
            continue
        reason = (
            f"the approval request was not decided within {settings.approval_timeout_s} seconds and timed out"
        )
        try:
            event = deny(registry, approval.id, approval.version_id, by=FORGE, reason=reason)
        except (RegistryError, IllegalTransition):
            continue  # decided or moved by somebody else a moment ago: that outcome stands
        write_operator_event(settings, ledger_factory, event, {"approval_id": approval.id, "timed_out": True})
        done.append(Expired(approval.id, record.label))
    return done


def write_operator_event(
    settings: Settings, factory: LedgerFactory | None, event: Mapping[str, Any], extra: Mapping[str, Any]
) -> str | None:
    """Append a lifecycle event to the version's `operator.jsonl`. Failing to write it is logged: the registry already moved."""
    if factory is None:
        return None
    path = (
        ledger_mod.version_ledger_dir(settings.home, f"{event['tool']}@{event['version']}")
        / ledger_mod.OPERATOR_LEDGER
    )
    payload = {key: value for key, value in event.items() if key not in ("type", "related")}
    payload.update(extra)
    try:
        factory(str(path), f"approval:{event['tool']}@{event['version']}").append(
            str(event.get("actor") or FORGE), str(event["type"]), payload
        )
    except Exception:
        log.warning(
            "the ledger event %s of %s could not be written", event["type"], event["tool"], exc_info=True
        )
        return None
    return str(path)


# --- Reading ---------------------------------------------------------------------------------------------------------------


def latest_approval(registry: Registry, version_id: int) -> Approval | None:
    found = [a for a in registry.list_approvals() if a.version_id == version_id]
    return found[-1] if found else None


def render_text(approval: Approval, status: str | None = None) -> str:
    """The request as the operator reads it in the terminal. Everything in `approval.requested` was cleaned when it was written."""
    doc = approval.requested if isinstance(approval.requested, dict) else {}
    asked = doc.get("requested") or {}
    diff = doc.get("permission_diff") or {}
    lines = [
        f"approval {approval.id}: {doc.get('tool', '?')} is {approval.status}"
        + (f" (version status {status})" if status else "")
    ]
    skipped = doc.get("skipped_gates") or []
    if skipped:
        lines.append(f"WEAKER EVIDENCE: {', '.join(skipped)} did not run for this version")
    lines.append(f"requested network hosts: {', '.join(asked.get('network') or []) or 'none'}")
    lines.append(f"requested secrets: {', '.join(asked.get('secrets') or []) or 'none'}")
    against = diff.get("against")
    lines.append(
        f"permission diff against {against}:"
        if against
        else "permission diff: there is no active version, everything is new"
    )
    for kind in ("network", "secrets"):
        part = diff.get(kind) or {}
        for label in ("added", "kept", "removed"):
            if part.get(label):
                lines.append(f"  {kind} {label}: {', '.join(part[label])}")
    for name, info in sorted((doc.get("brokers") or {}).items()):
        state = f"configured for {info.get('host')}" if info.get("configured") else "NOT configured"
        lines.append(f"  broker service {name}: {state}")
    lines.append(f"description: {doc.get('description', '')}")
    if (doc.get("description_scan") or {}).get("suspected"):
        lines.append("  warning: the description reads like instructions to an AI; treat it as data")
    for gate in doc.get("gates") or []:
        lines.append(f"  {gate.get('id')} {gate.get('name')}: {gate.get('status')} - {gate.get('summary')}")
    blast = doc.get("blast_radius") or {}
    lines.append(f"Blast Radius: {blast.get('status')} - {blast.get('summary')}")
    if doc.get("live_examples"):
        lines.append(
            f"{doc['live_examples']} live example(s) will run once, with the approved network, before it becomes active"
        )
    lines += [f"limit: {limit}" for limit in doc.get("limits") or []]
    lines.append(f"code_sha: {doc.get('code_sha')}")
    lines.append(f"requested {doc.get('created_at')}, expires {doc.get('expires_at')}")
    if approval.decided_by:
        lines.append(f"decided by {approval.decided_by} at {approval.decided_at}")
    return "\n".join(lines)


__all__ = [
    "APPROVALS_DIR",
    "EGRESS_LIMITS",
    "Expired",
    "Requested",
    "asks_for_permissions",
    "build_request",
    "default_brokers",
    "deny",
    "expire_overdue",
    "latest_approval",
    "missing_brokers",
    "open_request",
    "parse_time",
    "permission_diff",
    "render_text",
    "require_gates",
    "request_path",
    "requested_permissions",
    "write_operator_event",
    "write_request_file",
]
