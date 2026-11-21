"""Actions that only a person at the command line (or, from P17, the dashboard) may take. **The MCP server never imports this module.**

`promote` is the first one: it makes a verified version `active`, so agents can find and run it. It exists because an agent must
not be able to put its own code in front of other agents. The server package may not import any function named in
`toolforge.server.tools.OPERATOR_ONLY`; `tests/test_server_tools.py::test_server_never_imports_operator_actions` reads the server's
source and fails if it does. P08 added `release` and `quarantine` here, P09 adds `decide`, P12 `rollback`.

`quarantine` takes an active version out of use, and `release` puts a quarantined one back. Both need a reason, which goes to the
version's `operator.jsonl`. `release_context` is what the operator should read before releasing: why the version was quarantined
(from that ledger, as recorded) and its Blast Radius report. A runtime decoy touch quarantines a version by itself, as actor `forge`
(`services/runner.py`); only a person can release it. Releasing a version that has a newer active sibling deprecates that sibling,
as promoting does, and `release_context` says so.

`promote` refuses, in this order, and each message names the right next step:

* no reason (the reason goes into the ledger, so the history says why a human let this tool in);
* a version that does not exist, or whose name is not `name@version`;
* a version that is not `verified_basic` or `verified`: `active` already, `rejected`, `quarantined`, `deprecated`, `pending_approval`,
  and a version whose verification has not finished;
* a version with no stored package blob, or whose build gate (G2) did not pass: only a package that was really frozen and built can
  be run, and a registry row alone proves neither;
* a version whose manifest declares network hosts or secrets. Those need approval (P09); `promote` is never a way around it.

The status change is `lifecycle.transition(..., actor="operator", reason=...)`, which enforces the same rules again (this module only
gives better messages). The ledger event it returns (`forge.promoted`) is appended to the version's ledger, and so is the
`forge.deprecated` event of the version that was replaced, to that version's own ledger.

Proven by `tests/test_operator_actions.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from toolforge import ledger as ledger_mod
from toolforge.config import Settings
from toolforge.errors import IllegalTransition, LedgerError, RegistryError, ToolForgeError
from toolforge.registry import Registry, lifecycle

log = logging.getLogger(__name__)

OPERATOR = lifecycle.OPERATOR
PROMOTABLE = (lifecycle.VERIFIED_BASIC, lifecycle.VERIFIED)
LEDGER_FILE = ledger_mod.OPERATOR_LEDGER

LedgerFactory = Callable[[str | None, str], Any]


@dataclass(frozen=True)
class Promoted:
    """What `promote` did. `replaced` lists the versions that were deprecated because this one took their place."""

    label: str
    from_status: str
    replaced: tuple[str, ...] = ()
    ledger_files: tuple[str, ...] = field(default=())

    def text(self) -> str:
        lines = [f"{self.label} is now active (was {self.from_status})."]
        if self.replaced:
            lines.append(f"deprecated: {', '.join(self.replaced)}")
        return "\n".join(lines)


def parse_label(text: object) -> tuple[str, str]:
    """`name@version` as `(name, version)`. Raises `RegistryError` for anything else."""
    if not isinstance(text, str) or text.count("@") != 1:
        raise RegistryError(
            "name the version as name@version",
            code="bad_version_ref",
            hint="for example: toolforge promote csv_summary@1.0.0 --reason 'checked by hand'",
        )
    name, _, version = text.strip().partition("@")
    if not name or not version:
        raise RegistryError("name the version as name@version", code="bad_version_ref")
    return name, version


def _refuse_status(label: str, status: str) -> ToolForgeError:
    """The refusal for a version that cannot be promoted from where it is, with the command that does apply."""
    why: dict[str, tuple[str, str]] = {
        lifecycle.ACTIVE: (f"{label} is already active", "nothing to do"),
        lifecycle.REJECTED: (
            f"{label} was rejected by verification and can never be promoted",
            "fix the package, raise its version and submit it again",
        ),
        lifecycle.QUARANTINED: (
            f"{label} is quarantined, which is not the same as verified",
            "a quarantined version returns with `toolforge release <name@version> --reason ...`, after you have read `toolforge report <name@version>`",
        ),
        lifecycle.DEPRECATED: (
            f"{label} is deprecated: a newer version replaced it",
            "going back to an older version is `toolforge rollback`, which arrives in P12",
        ),
        lifecycle.PENDING_APPROVAL: (
            f"{label} is waiting for approval and promote does not replace approval",
            "approvals arrive in P09; the operator decides there",
        ),
        lifecycle.SUBMITTED: (
            f"{label} has not been verified yet (status submitted)",
            "run `toolforge submit` again to finish verification, then promote",
        ),
        lifecycle.VERIFYING: (
            f"{label} is still being verified (status verifying)",
            "wait for verification to finish, or run `toolforge submit` on the same package to resume it",
        ),
    }
    message, hint = why.get(
        status, (f"{label} is {status} and cannot be promoted", "promote takes verified_basic or verified")
    )
    return IllegalTransition(message, code="not_promotable", hint=hint)


def _require_built(registry: Registry, record: Any) -> None:
    """Refuse a version that has no stored blob or whose latest G2 row is not `passed`."""
    builds = [g for g in registry.gate_results(record.id) if g.gate == "G2"]
    if not builds or builds[-1].status != "passed":
        found = builds[-1].status if builds else "no result"
        raise IllegalTransition(
            f"{record.label} cannot be promoted: its build gate (G2) did not pass ({found})",
            code="not_built",
            hint="submit the package again so that verification can finish, then promote",
        )
    if not record.blob_sha or not registry.blobs.exists(record.blob_sha):
        raise IllegalTransition(
            f"{record.label} cannot be promoted: its frozen package is not in the store",
            code="not_built",
            hint="submit the package again so that verification can store it, then promote",
        )


def promote(
    settings: Settings,
    registry: Registry,
    ref: str,
    reason: str,
    *,
    ledger_factory: LedgerFactory | None = None,
) -> Promoted:
    """Make a verified version active. `ref` is `name@version`. See the module docstring for what it refuses.

    The version becomes active in the registry first, in one transaction. A failure to write the ledger afterwards is raised as
    `LedgerError` whose message says the change is already made, because the registry is the truth and the ledger is the history.
    """
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise IllegalTransition(
            "promote needs a reason",
            code="reason_required",
            hint='say why, for example --reason "reviewed the code by hand"; it is written to the ledger',
        )
    name, version = parse_label(ref)
    record = registry.find_version(name, version)
    if record is None:
        raise RegistryError(
            f"no version {name}@{version}",
            code="unknown_version",
            hint="list what exists with `toolforge registry list`",
        )
    if record.status not in PROMOTABLE:
        raise _refuse_status(record.label, record.status)
    _require_built(registry, record)
    if record.asks_network_or_secrets:
        raise IllegalTransition(
            f"{record.label} asks for network access or secrets: tools that ask for network or secrets need approval, which arrives in P09",
            code="needs_approval",
            hint="promote does not replace approval; until P09 such a version cannot become active",
        )
    event = registry.transition(record.id, lifecycle.ACTIVE, actor=OPERATOR, reason=text)
    written = _write_ledger(settings, event, ledger_factory)
    replaced = tuple(f"{e['tool']}@{e['version']}" for e in event.get("related", []))
    return Promoted(record.label, record.status, replaced, tuple(written))


def _write_ledger(
    settings: Settings, event: dict[str, Any], factory: LedgerFactory | None, what: str = "active"
) -> list[str]:
    """Append a promotion, quarantine or release event, and the deprecation events it caused, to the ledgers of their versions."""
    if factory is None:
        return []
    related = list(event.get("related", []))
    events = [event, *related]
    written: list[str] = []
    try:
        for item in events:
            payload = {key: value for key, value in item.items() if key not in ("type", "related")}
            if item is event and related:
                payload["replaced"] = [f"{e['tool']}@{e['version']}" for e in related]
            path = Path(settings.home) / "ledgers" / f"{item['tool']}@{item['version']}" / LEDGER_FILE
            factory(str(path), f"operator:{item['tool']}@{item['version']}").append(
                OPERATOR, item["type"], payload
            )
            written.append(str(path))
    except Exception as exc:
        log.warning("the ledger could not be written after a promotion", exc_info=True)
        raise LedgerError(
            f"{event['tool']}@{event['version']} is now {what}, but its ledger event could not be written ({type(exc).__name__})",
            hint="the registry is correct; check that the ledger folder under $TOOLFORGE_HOME is writable and run `toolforge doctor`",
        ) from exc
    return written


@dataclass(frozen=True)
class Moved:
    """What `quarantine` or `release` did."""

    label: str
    action: str  # "quarantined" | "released"
    from_status: str
    replaced: tuple[str, ...] = ()
    ledger_files: tuple[str, ...] = field(default=())

    def text(self) -> str:
        if self.action == "quarantined":
            return (
                f"{self.label} is now quarantined (was {self.from_status}). It can no longer be found or run."
            )
        lines = [f"{self.label} is active again (was {self.from_status})."]
        if self.replaced:
            lines.append(f"deprecated: {', '.join(self.replaced)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ReleaseContext:
    """What to read before `release`: the quarantine as recorded, the Blast Radius summary and any version that would be replaced."""

    label: str
    status: str
    quarantine: tuple[str, ...]  # recorded quarantine reasons, newest last (empty: none were recorded)
    blast_radius: dict[str, Any]
    active_sibling: str | None = None

    def text(self) -> str:
        lines = [f"{self.label} is {self.status}."]
        if self.quarantine:
            lines += [f"quarantine reason: {reason}" for reason in self.quarantine]
        else:
            lines.append("quarantine reason: none recorded in the operator ledger")
        lines.append(f"Blast Radius: {self.blast_radius.get('status')} - {self.blast_radius.get('summary')}")
        lines += [f"  violation: {item}" for item in self.blast_radius.get("violations") or []]
        if self.active_sibling:
            lines.append(f"releasing deprecates {self.active_sibling}, which is active now")
        return "\n".join(lines)


def _need_reason(verb: str, reason: object) -> str:
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise IllegalTransition(
            f"{verb} needs a reason",
            code="reason_required",
            hint='say why, for example --reason "reviewed the report"; it is written to the ledger',
        )
    return text


def _find(registry: Registry, ref: str) -> Any:
    name, version = parse_label(ref)
    record = registry.find_version(name, version)
    if record is None:
        raise RegistryError(
            f"no version {name}@{version}",
            code="unknown_version",
            hint="list what exists with `toolforge registry list`",
        )
    return record


def quarantine(
    settings: Settings,
    registry: Registry,
    ref: str,
    reason: str,
    *,
    ledger_factory: LedgerFactory | None = None,
) -> Moved:
    """Take an active version out of use. Only `active` can be quarantined; the refusal for any other status names the right step."""
    text = _need_reason("quarantine", reason)
    record = _find(registry, ref)
    if record.status != lifecycle.ACTIVE:
        raise IllegalTransition(
            f"{record.label} is {record.status}; only an active version can be quarantined",
            code="not_quarantinable",
            hint="a version that is not active cannot be run, so there is nothing to stop"
            if record.status != lifecycle.QUARANTINED
            else "it is quarantined already",
        )
    event = registry.transition(record.id, lifecycle.QUARANTINED, actor=OPERATOR, reason=text)
    written = _write_ledger(settings, event, ledger_factory, what="quarantined")
    return Moved(record.label, "quarantined", record.status, (), tuple(written))


def release(
    settings: Settings,
    registry: Registry,
    ref: str,
    reason: str,
    *,
    ledger_factory: LedgerFactory | None = None,
) -> Moved:
    """Put a quarantined version back in use. The operator should have read `release_context` first (the CLI prints it)."""
    text = _need_reason("release", reason)
    record = _find(registry, ref)
    if record.status != lifecycle.QUARANTINED:
        hints = {
            lifecycle.ACTIVE: "it is active already",
            lifecycle.DEPRECATED: "going back to an older version is `toolforge rollback`, which arrives in P12",
            lifecycle.VERIFIED: "a verified version is made active with `toolforge promote`",
            lifecycle.VERIFIED_BASIC: "a verified version is made active with `toolforge promote`",
        }
        raise IllegalTransition(
            f"{record.label} is {record.status}; only a quarantined version can be released",
            code="not_releasable",
            hint=hints.get(record.status, "release applies to quarantined versions only"),
        )
    event = registry.transition(record.id, lifecycle.ACTIVE, actor=OPERATOR, reason=text)
    written = _write_ledger(settings, event, ledger_factory, what="active again")
    replaced = tuple(f"{e['tool']}@{e['version']}" for e in event.get("related", []))
    return Moved(record.label, "released", record.status, replaced, tuple(written))


def _recorded_quarantines(settings: Settings, label: str) -> tuple[str, ...]:
    """The reasons in the version's operator ledger, cleaned. A missing or unreadable ledger gives nothing, never an error."""
    from toolforge import textsafety

    path = ledger_mod.version_ledger_dir(settings.home, label) / LEDGER_FILE
    try:
        events, _ = ledger_mod.read_ledger(path)
    except Exception:  # a ledger that cannot be read must not stop a release decision from being shown
        return ()
    out: list[str] = []
    for event in events:
        if event.get("type") != "forge.quarantined":
            continue
        payload = event.get("payload") or {}
        who = textsafety.sanitize(str(event.get("actor") or "?"), max_chars=30)
        why = textsafety.sanitize(str(payload.get("reason") or "no reason recorded"), max_chars=300)
        out.append(f"{why} (by {who})")
    return tuple(out[-3:])


def release_context(settings: Settings, registry: Registry, ref: str) -> ReleaseContext:
    """Read-only: what the operator should see before releasing `ref`. Writes nothing."""
    from toolforge.report import blast_radius

    record = _find(registry, ref)
    document, problem = blast_radius.load_stored(settings, record.label)
    summary = blast_radius.summarize(document) if document else blast_radius.missing_summary(problem)
    sibling = registry.active_version(record.name)
    return ReleaseContext(
        record.label,
        record.status,
        _recorded_quarantines(settings, record.label),
        summary,
        sibling.label if sibling is not None and sibling.id != record.id else None,
    )


__all__ = [
    "Moved",
    "PROMOTABLE",
    "Promoted",
    "ReleaseContext",
    "parse_label",
    "promote",
    "quarantine",
    "release",
    "release_context",
]
