"""What the dashboard shows, as plain JSON (P17).

Every function here only reads: the registry, the stored Blast Radius report and the ledger files. Nothing here changes a tool's state;
that is `toolforge.services.operator_actions`, called from `app.py` and nowhere else.

Tool-authored text (descriptions, gate summaries, hints, examples, a version's manifest) is cleaned with the same function the MCP
server uses before it leaves the process: invisible and control characters are removed and long text is cut. It is still untrusted
text. The page puts it on the screen with `textContent` only, never as HTML, and a description the injection scanner suspects carries a
`flagged` badge.

A number that was not measured is `null` with `measured: false`, never `0`.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from typing import Any

from toolforge import ledger as ledger_mod
from toolforge.adapters.browser_plan import PLAN_KIND
from toolforge.config import Settings
from toolforge.errors import SandboxUnavailable
from toolforge.registry import CallRow, Registry, lifecycle
from toolforge.registry.db import VersionRecord
from toolforge.report import blast_radius
from toolforge.server.fences import clean_json, clean_text, description_flags
from toolforge.services import approvals, operator_actions
from toolforge.textsafety import sanitize
from toolforge.verify.pipeline import GATE_INFO, report_from_registry

SHORT_DESCRIPTION = 240
MAX_MANIFEST_CHARS = 60_000
MAX_GATE_HISTORY = 60
MAX_PAGE = 200
DEFAULT_PAGE = 50


# --- small helpers -----------------------------------------------------------------------------------------------------


def page_args(
    limit: Any, offset: Any, *, default: int = DEFAULT_PAGE, cap: int = MAX_PAGE
) -> tuple[int, int]:
    """A limit and an offset a query string can ask for, kept inside sane bounds."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    try:
        start = int(offset)
    except (TypeError, ValueError):
        start = 0
    return max(1, min(n, cap)), max(0, start)


def public_session(value: Any) -> str | None:
    """The part of a session id before the first dot. A caller can pass a full session id to a call, so only this half is ever shown."""
    if not isinstance(value, str) or not value:
        return None
    return sanitize(value.partition(".")[0], max_chars=40)


def _stats(registry: Registry, version_id: int) -> dict[str, Any]:
    s = registry.stats(version_id)
    return {
        "uses": s.uses,
        "recent_calls": s.recent_calls,
        "success_rate": s.success_rate,
        "measured": s.success_rate is not None,
    }


def _flags(*texts: str) -> dict[str, Any]:
    flags, scan = description_flags(*[t for t in texts if t])
    return {"flags": flags, "flagged": bool(flags), "injection_scan": scan}


def _description(manifest: dict[str, Any], cap: int = SHORT_DESCRIPTION) -> str:
    return clean_text(manifest.get("description", ""), cap)


def meta(version: str) -> dict[str, Any]:
    """What the page needs to draw itself: the lifecycle statuses, the gates in plain language, and the version."""
    return {
        "toolforge_version": version,
        "statuses": list(lifecycle.STATUSES),
        "gates": [
            {"id": g.id, "name": g.name, "summary": g.plain_summary, "status": g.status} for g in GATE_INFO
        ],
    }


# --- registry ----------------------------------------------------------------------------------------------------------


def tool_list(
    registry: Registry, *, query: str = "", status: str = "", limit: Any = None, offset: Any = None
) -> dict[str, Any]:
    """Every tool with its active version, its newest version and how many versions are in each status. `query` is a case-insensitive
    substring of the name or description; `status` keeps tools that have a version in that status."""
    n, start = page_args(limit, offset, default=100, cap=500)
    needle = clean_text(query, 80).lower()
    wanted = clean_text(status, 40)
    by_name: dict[str, list[VersionRecord]] = defaultdict(list)
    for record in registry.list_versions():
        by_name[record.name].append(record)
    rows: list[dict[str, Any]] = []
    for tool in registry.list_tools():
        versions = by_name.get(tool.name, [])
        counts = Counter(v.status for v in versions)
        if wanted and not counts.get(wanted):
            continue
        active = next(
            (v for v in versions if v.status == lifecycle.ACTIVE and v.version == tool.active_version), None
        )
        shown = active or (versions[-1] if versions else None)
        description = _description(shown.manifest) if shown else ""
        if needle and needle not in tool.name.lower() and needle not in description.lower():
            continue
        row: dict[str, Any] = {
            "name": tool.name,
            "active_version": tool.active_version if active else None,
            "latest_version": versions[-1].version if versions else None,
            "latest_status": versions[-1].status if versions else None,
            "kind": _kind(shown.manifest) if shown else None,
            "versions": len(versions),
            "status_counts": dict(counts),
            "description": description,
            "created_at": tool.created_at,
            **_flags(description),
        }
        row["stats"] = (
            _stats(registry, active.id)
            if active
            else {"uses": 0, "recent_calls": 0, "success_rate": None, "measured": False}
        )
        rows.append(row)
    return {"total": len(rows), "offset": start, "limit": n, "tools": rows[start : start + n]}


def tool_detail(registry: Registry, name: str) -> dict[str, Any] | None:
    versions = registry.list_versions(name)
    tool = registry.get_tool(name)
    if tool is None and not versions:
        return None
    active = tool.active_version if tool else None
    rows = []
    for v in versions:
        rows.append(
            {
                "version": v.version,
                "status": v.status,
                "parent_version": v.parent_version,
                "created_by": clean_text(v.created_by, 80),
                "created_at": v.created_at,
                "updated_at": v.updated_at,
                "needs_approval": v.asks_network_or_secrets,
                "kind": _kind(v.manifest),
                "description": _description(v.manifest),
                "stats": _stats(registry, v.id),
            }
        )
    known = {v.version for v in versions}
    return {
        "name": clean_text(name, 80),
        "active_version": active,
        "versions": rows,
        # The lineage: each version and the version it was derived from (`null` for a first version, and for a parent that is gone).
        "lineage": [
            {
                "version": v.version,
                "parent": v.parent_version if v.parent_version in known else None,
                "status": v.status,
            }
            for v in versions
        ],
    }


def _kind(manifest: dict[str, Any]) -> str:
    return clean_text(manifest.get("kind") or "stateless", 40)


def _permissions(record: VersionRecord) -> dict[str, Any]:
    """Network hosts and secret names, plus whether the tool drives a browser (a browser plan opens pages through the egress proxy)."""
    wanted = approvals.requested_permissions(record.manifest)
    out: dict[str, Any] = {k: [clean_text(x, 200) for x in v] for k, v in wanted.items()}
    perms = record.manifest.get("permissions")
    out["browser"] = bool(isinstance(perms, dict) and perms.get("browser") is True)
    return out


def available_actions(record: VersionRecord) -> list[str]:
    """Which operator buttons apply to a version in its status. The server refuses the rest, whatever the page shows."""
    status = record.status
    out: list[str] = []
    if status in operator_actions.PROMOTABLE and not record.asks_network_or_secrets:
        out.append("promote")
    if status == lifecycle.ACTIVE:
        out.append("quarantine")
    if status == lifecycle.QUARANTINED:
        out.append("release")
    if status == lifecycle.DEPRECATED:
        out.append("rollback")
    if status == lifecycle.PENDING_APPROVAL:
        out.append("decide")
    return out


def _manifest_view(manifest: dict[str, Any]) -> dict[str, Any]:
    cleaned = clean_json(manifest, 600)
    if len(json.dumps(cleaned, default=str)) <= MAX_MANIFEST_CHARS:
        return {"manifest": cleaned, "manifest_cut": False}
    keep = ("name", "version", "description", "when_to_use", "permissions")
    return {"manifest": {k: cleaned[k] for k in keep if k in cleaned}, "manifest_cut": True}


def _gate_history(registry: Registry, record: VersionRecord) -> list[dict[str, Any]]:
    rows = registry.gate_results(record.id)[-MAX_GATE_HISTORY:]
    out = []
    for r in rows:
        detail = r.detail if isinstance(r.detail, dict) else {}
        out.append(
            {
                "gate": r.gate,
                "status": r.status,
                "summary": clean_text(detail.get("summary", ""), 300),
                "finished_at": r.finished_at,
            }
        )
    return out


def call_view(row: CallRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "version": row.label,
        "ok": row.ok,
        "duration_ms": row.duration_ms,
        "error_class": clean_text(row.error_class, 60) if row.error_class else None,
        "session": public_session(row.session_id),
        "ts": row.ts,
    }


def version_detail(registry: Registry, record: VersionRecord) -> dict[str, Any]:
    report = report_from_registry(registry, record)
    gates = [
        {
            "id": g.id,
            "name": g.name,
            "status": g.status,
            "summary": clean_text(g.summary, 300),
            "hints": [clean_text(h, 300) for h in g.hints],
            "metrics": clean_json(g.metrics, 200),
        }
        for g in report.gates
    ]
    manifest = record.manifest
    description = clean_text(manifest.get("description", ""), 600)
    when = clean_text(manifest.get("when_to_use", ""), 600)
    badges = _flags(description, when, *[h for g in gates for h in g["hints"]])
    latest = approvals.latest_approval(registry, record.id)
    return {
        "name": record.name,
        "version": record.version,
        "label": record.label,
        "status": record.status,
        "code_sha": record.code_sha,
        "parent_version": record.parent_version,
        "created_by": clean_text(record.created_by, 80),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "kind": _kind(manifest),
        "description": description,
        "when_to_use": when,
        "permissions": _permissions(record),
        # What a browser plan will do, one line per step, read from the frozen package (None for any other kind of tool).
        "plan": [clean_text(line, 400) for line in approvals.plan_lines(registry, record)]
        if manifest.get("kind") == PLAN_KIND
        else None,
        "needs_approval": record.asks_network_or_secrets,
        "gates": gates,
        "gate_history": _gate_history(registry, record),
        "next_action": clean_text(report.next_action, 600),
        "approval": {"id": latest.id, "status": latest.status} if latest else None,
        "open_sessions": len(registry.open_sessions_of(record.id)),
        "stats": _stats(registry, record.id),
        "actions": available_actions(record),
        "recent_calls": [call_view(c) for c in registry.recent_calls(record.id, limit=10)],
        **badges,
        **_manifest_view(manifest),
    }


def calls_page(
    registry: Registry, record: VersionRecord, *, limit: Any = None, before: Any = None
) -> dict[str, Any]:
    n, _ = page_args(limit, 0, default=20, cap=100)
    try:
        before_id = int(before) if before is not None else None
    except (TypeError, ValueError):
        before_id = None
    rows = registry.recent_calls(record.id, limit=n, before_id=before_id)
    return {
        "calls": [call_view(r) for r in rows],
        "next_before": rows[-1].id if len(rows) == n else None,
    }


# --- Blast Radius report -----------------------------------------------------------------------------------------------


def stored_report(settings: Settings, record: VersionRecord) -> dict[str, Any]:
    """The stored report as `{"stored": bool, "summary": ..., "document": ...}`. A version with none says so; nothing is generated."""
    document, problem = blast_radius.load_stored(settings, record.label)
    if document is None:
        return {"stored": False, "summary": blast_radius.missing_summary(problem), "document": None}
    return {
        "stored": True,
        "summary": clean_json(blast_radius.summarize(document), 600),
        "document": clean_json(document, 600),
    }


def report_html(settings: Settings, record: VersionRecord) -> str | None:
    """MiniLocker's own page for the stored report, or None when no usable report is stored."""
    document, _ = blast_radius.load_stored(settings, record.label)
    return None if document is None else blast_radius.render_html(document)


# --- ledger verification -----------------------------------------------------------------------------------------------


def ledger_verify(
    settings: Settings, record: VersionRecord, *, limit: Any = None, offset: Any = None
) -> dict[str, Any]:
    """Re-verify the hash chain of one version's ledger files, a page of files at a time. Reads only; it never repairs anything."""
    n, start = page_args(limit, offset, default=25, cap=100)
    directory = ledger_mod.version_ledger_dir(settings.home, record.label)
    try:
        files = sorted(p for p in directory.glob("*.jsonl") if p.is_file() and not p.is_symlink())
    except OSError:
        files = []
    out: dict[str, Any] = {"label": record.label, "files_total": len(files), "offset": start, "limit": n}
    rows: list[dict[str, Any]] = []
    try:
        for path in files[start : start + n]:
            events, problem = ledger_mod.read_ledger(path)
            ok, message = ledger_mod.verify_chain(events) if events else (False, "no events")
            rows.append(
                {
                    "file": sanitize(path.name, max_chars=120),
                    "events": len(events),
                    "chain_ok": ok and problem is None,
                    "message": clean_text(message, 300),
                    "problem": clean_text(problem, 300) if problem else None,
                }
            )
    except SandboxUnavailable as exc:
        return {**out, "measured": False, "reason": clean_text(str(exc), 300), "files": []}
    return {
        **out,
        "measured": True,
        "files": rows,
        "all_ok_on_page": bool(rows) and all(r["chain_ok"] for r in rows),
    }


# --- approvals ---------------------------------------------------------------------------------------------------------


def approval_view(registry: Registry, approval: Any, *, with_text: bool = False) -> dict[str, Any]:
    record = registry.get_version(approval.version_id)
    doc = clean_json(approval.requested, 600) if isinstance(approval.requested, dict) else {}
    out = {
        "id": approval.id,
        "status": approval.status,
        "label": record.label if record else None,
        "version_status": record.status if record else None,
        "decided_by": clean_text(approval.decided_by, 80) if approval.decided_by else None,
        "decided_at": approval.decided_at,
        "created_at": approval.created_at,
        "requested": doc,
    }
    if with_text:
        out["text"] = clean_text(approvals.render_text(approval, record.status if record else None), 6000)
    return out


# --- sessions ----------------------------------------------------------------------------------------------------------


def session_view(row: Any, now: float) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "label": row.label,
        "state": row.state,
        "reason": clean_text(row.reason, 200) if row.reason else None,
        "steps": row.steps,
        "rewinds": row.rewinds,
        "close_requested": row.close_requested,
        "orphaned": row.orphaned(now),
        "age_s": max(0, int(now - row.opened_at)),
        "idle_s": max(0, int(now - row.last_used_at)),
        "left_s": max(0, int(min(row.expires_at, row.hard_expires_at) - now)) if row.is_open else 0,
    }


def sessions_list(registry: Registry, *, state: str = "open", limit: Any = None) -> dict[str, Any]:
    n, _ = page_args(limit, 0, default=100, cap=200)
    now = time.time()
    rows = registry.list_sessions(state="open" if state == "open" else None, limit=n)
    return {"sessions": [session_view(r, now) for r in rows]}


# --- stored attack and benchmark results are read in results.py --------------------------------------------------------

__all__ = [
    "approval_view",
    "available_actions",
    "call_view",
    "calls_page",
    "ledger_verify",
    "meta",
    "page_args",
    "public_session",
    "report_html",
    "session_view",
    "sessions_list",
    "stored_report",
    "tool_detail",
    "tool_list",
    "version_detail",
]
