"""Actions that only a person at the command line (or, from P17, the dashboard) may take. **The MCP server never imports this module.**

`promote` is the first one: it makes a verified version `active`, so agents can find and run it. It exists because an agent must
not be able to put its own code in front of other agents. The server package may not import any function named in
`toolforge.server.tools.OPERATOR_ONLY`; `tests/test_server_tools.py::test_server_never_imports_operator_actions` reads the server's
source and fails if it does. P08 added `release` and `quarantine` here, P09 added `decide`, P12 added `rollback`.

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

`decide` answers an approval request (P09): `approve` makes a `pending_approval` version active, `deny` rejects it. Approving refuses,
in this order: no reason; a request that was decided or has timed out; a version that is not `pending_approval` any more or whose
`code_sha` is not the one the request recorded; a stored package that was never built; a secret with no broker service in this
environment. Then any example marked `live` runs once in a sandbox with the approved network (and the brokered services) and its
failure rejects the version instead; a sandbox that cannot start changes nothing. The decision and the move to `active` are one
registry transaction. `promote` is never a way around it, and approval is the only way a version with network or secrets becomes active.

`rollback` makes a **previously active** version active again (P12). The only versions that were ever active and are not now are the
`deprecated` ones (the lifecycle reaches `deprecated` only from `active`), so rollback takes exactly those: such a version already passed
every gate and any approval it needed, and approvals are bound to its `code_sha`, so none is repeated. The version that is active now, if
any, becomes `deprecated`; a quarantined one stays quarantined. A version that was never active is refused with the command that does
apply (`promote`, `release`, `approvals`), and so is one whose stored package is gone or whose network or secrets have no recorded approval
for this content. It needs a reason, which is written with `forge.rolled_back` to the version's `operator.jsonl`. No MCP tool can do it.

Proven by `tests/test_operator_actions.py`, `tests/test_decide.py` and `tests/test_rollback.py`.
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
from toolforge.services import approvals

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
            "going back to an older version is `toolforge rollback <name> --to <version> --reason ...`",
        ),
        lifecycle.PENDING_APPROVAL: (
            f"{label} is waiting for approval and promote does not replace approval",
            "read the request with `toolforge approvals show <id>`, then `toolforge approvals approve|deny <id> --reason ...`",
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
            f"{record.label} asks for network access or secrets: tools that ask for network or secrets need approval",
            code="needs_approval",
            hint="promote does not replace approval; submit it so it reaches pending_approval, then use `toolforge approvals`",
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
    action: str  # "quarantined" | "released" | "rolled_back"
    from_status: str
    replaced: tuple[str, ...] = ()
    ledger_files: tuple[str, ...] = field(default=())

    def text(self) -> str:
        if self.action == "quarantined":
            return (
                f"{self.label} is now quarantined (was {self.from_status}). It can no longer be found or run."
            )
        verb = "is active again after a rollback" if self.action == "rolled_back" else "is active again"
        lines = [f"{self.label} {verb} (was {self.from_status})."]
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
            lifecycle.DEPRECATED: "going back to an older version is `toolforge rollback <name> --to <version> --reason ...`",
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


_NEVER_ACTIVE: dict[str, str] = {
    lifecycle.VERIFIED: "it passed every gate but was never made active: `toolforge promote {label} --reason ...`",
    lifecycle.VERIFIED_BASIC: "it passed the gates that could run but was never made active: `toolforge promote {label} --reason ...`",
    lifecycle.PENDING_APPROVAL: "it is waiting for approval: `toolforge approvals show <id>`, then `toolforge approvals approve|deny <id> --reason ...`",
    lifecycle.QUARANTINED: "it is quarantined, which is not the same as replaced: read `toolforge report {label}`, then `toolforge release {label} --reason ...`",
    lifecycle.REJECTED: "it was rejected by verification and can never run",
    lifecycle.SUBMITTED: "it has not been verified yet",
    lifecycle.VERIFYING: "it is still being verified",
}


def rollback(
    settings: Settings,
    registry: Registry,
    name: str,
    to_version: str,
    reason: str,
    *,
    ledger_factory: LedgerFactory | None = None,
) -> Moved:
    """Make a version that was active before active again. See the module docstring for what it refuses."""
    text = _need_reason("rollback", reason)
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(to_version, str)
        or not to_version.strip()
    ):
        raise RegistryError(
            "name the tool and the version to go back to",
            code="bad_version_ref",
            hint='for example: toolforge rollback csv_summary --to 1.0.0 --reason "1.1.0 gives wrong totals"',
        )
    record = registry.find_version(name.strip(), to_version.strip())
    if record is None:
        known = ", ".join(v.version for v in registry.list_versions(name.strip())[-10:]) or "none"
        raise RegistryError(
            f"no version {name.strip()}@{to_version.strip()}",
            code="unknown_version",
            hint=f"the versions of {name.strip()}: {known}",
        )
    if record.status == lifecycle.ACTIVE:
        raise IllegalTransition(
            f"{record.label} is already active", code="not_rollbackable", hint="nothing to do"
        )
    if record.status != lifecycle.DEPRECATED:
        why = _NEVER_ACTIVE.get(record.status, f"it is {record.status}").format(label=record.label)
        raise IllegalTransition(
            f"{record.label} was never active, so there is nothing to roll back to: {why}",
            code="never_active",
            hint="rollback only returns to a version that already passed every gate and approval while it was in use",
        )
    if not record.blob_sha or not registry.blobs.exists(record.blob_sha):
        raise IllegalTransition(
            f"{record.label} cannot be rolled back to: its frozen package is not in the store",
            code="not_built",
            hint="submit the tool again as a new version instead",
        )
    if record.asks_network_or_secrets:
        asked = {
            "network": sorted(str(h) for h in record.permissions.get("network") or []),
            "secrets": sorted(str(n) for n in record.permissions.get("secrets") or []),
        }
        approved = [
            a
            for a in registry.list_approvals("approved")
            if a.version_id == record.id
            and (a.requested or {}).get("code_sha") == record.code_sha
            and (a.requested or {}).get("requested") == asked
        ]
        if not approved:
            raise IllegalTransition(
                f"{record.label} asks for network hosts or secrets and has no recorded approval for this content",
                code="needs_approval",
                hint="submit it again as a new version so the operator can approve what it asks for (`toolforge approvals`)",
            )
    event = registry.transition(record.id, lifecycle.ACTIVE, actor=OPERATOR, reason=text)
    written = _write_ledger(settings, event, ledger_factory, what="active again")
    replaced = tuple(f"{e['tool']}@{e['version']}" for e in event.get("related", []))
    return Moved(record.label, "rolled_back", record.status, replaced, tuple(written))


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


# --- Approvals (G7) -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decided:
    """What `decide` did. `outcome` is `approved`, `denied` or `rejected_by_smoke` (approval was asked for and a live example failed)."""

    label: str
    approval_id: int
    outcome: str
    to_status: str
    replaced: tuple[str, ...] = ()
    smoke: tuple[str, ...] = ()
    ledger_files: tuple[str, ...] = field(default=())

    def text(self) -> str:
        lines = {
            "approved": f"{self.label} is approved and now active.",
            "denied": f"{self.label} was denied and is rejected.",
            "rejected_by_smoke": f"{self.label} was not approved: a live example failed, so it is rejected.",
        }[self.outcome].splitlines()
        lines += [f"live example: {item}" for item in self.smoke]
        if self.replaced:
            lines.append(f"deprecated: {', '.join(self.replaced)}")
        return "\n".join(lines)


ToolRunnerFactory = Callable[[], Any]
BrokerLookup = Callable[[], Any]


def _live_examples(record: Any) -> list[dict[str, Any]]:
    return [e for e in record.manifest.get("examples") or [] if isinstance(e, dict) and e.get("live")]


def _run_live(
    registry: Registry, record: Any, factory: ToolRunnerFactory | None
) -> tuple[bool, tuple[str, ...]]:
    """Run each live example once. `(all passed, one line per example)`. A sandbox that cannot start raises: nothing was decided."""
    examples = _live_examples(record)
    if not examples:
        return True, ()
    if factory is None:
        raise IllegalTransition(
            f"{record.label} has live examples, which run once in a sandbox before it becomes active, and no sandbox was given",
            code="smoke_needs_sandbox",
            hint="run `toolforge approvals approve` from the command line with Docker available",
        )
    runner = factory()
    lines: list[str] = []
    passed = True
    for number, example in enumerate(examples, start=1):
        files = {str(k): str(v).encode("utf-8") for k, v in (example.get("files") or {}).items()}
        result = runner.run(
            record,
            dict(example.get("input") or {}),
            files=files or None,
            allow_status=(lifecycle.PENDING_APPROVAL,),
            record_stats=False,
        )
        if result.ok:
            lines.append(f"#{number}: ok")
        else:
            passed = False
            kind = result.error.type if result.error else "unknown"
            message = result.error.message[:160] if result.error else ""
            lines.append(f"#{number}: failed ({kind}) {message}".strip())
    return passed, tuple(lines)


def decide(
    settings: Settings,
    registry: Registry,
    approval_id: int,
    decision: str,
    reason: str,
    *,
    tool_runner_factory: ToolRunnerFactory | None = None,
    brokers: BrokerLookup | None = None,
    ledger_factory: LedgerFactory | None = None,
) -> Decided:
    """Answer an approval request: `decision` is `approve` or `deny`. See the module docstring for what approving refuses.

    `tool_runner_factory()` returns the `ToolRunner` that runs live examples (only called when there are any). `brokers()` says which
    brokered services exist (`{name: host}`); the default asks MiniLocker. The ledger events go to the version's `operator.jsonl`.
    """
    if decision not in ("approve", "deny"):
        raise IllegalTransition("a decision is approve or deny", code="bad_decision")
    text = _need_reason(decision, reason)
    approvals.expire_overdue(settings, registry, ledger_factory=ledger_factory)
    approval = registry.get_approval(approval_id)
    if approval is None:
        raise RegistryError(
            f"no approval request with id {approval_id}",
            code="unknown_approval",
            hint="list them with `toolforge approvals list`",
        )
    if approval.status != "pending":
        raise IllegalTransition(
            f"approval {approval_id} was already {approval.status}; a decision cannot be changed",
            code="approval_decided",
            hint="a denied or timed-out version is rejected for good: raise its version and submit it again",
        )
    record = registry.get_version(approval.version_id)
    if record is None or record.status != lifecycle.PENDING_APPROVAL:
        found = record.status if record is not None else "missing"
        raise IllegalTransition(
            f"approval {approval_id} belongs to a version that is {found}, not pending_approval",
            code="not_pending",
            hint="nothing to decide",
        )
    if decision == "deny":
        event = approvals.deny(registry, approval_id, record.id, by=OPERATOR, reason=text)
        written = approvals.write_operator_event(
            settings, ledger_factory, event, {"approval_id": approval_id}
        )
        return Decided(
            record.label,
            approval_id,
            "denied",
            lifecycle.REJECTED,
            ledger_files=(written,) if written else (),
        )

    recorded = (approval.requested or {}).get("code_sha")
    if recorded != record.code_sha:
        raise IllegalTransition(
            f"approval {approval_id} was requested for different content than {record.label} has now",
            code="code_changed",
            hint="deny it; a changed package is a new version with a new request",
        )
    _require_built(registry, record)
    approvals.require_gates(registry, record)
    asked = approvals.requested_permissions(record.manifest)
    if asked["secrets"]:
        known, problem = (
            (dict(brokers()), None) if brokers is not None else approvals.default_brokers(settings)
        )
        absent = approvals.missing_brokers(asked["secrets"], known)
        if absent:
            raise IllegalTransition(
                f"{record.label} needs a brokered secret for {', '.join(absent)} and none is configured here"
                + (f" ({problem})" if problem else ""),
                code="broker_not_configured",
                hint="export MINILOCKER_BROKER=<name>=<host> and MINILOCKER_BROKER_TOKEN_<NAME> in this shell, then approve again",
            )
    passed, smoke = _run_live(registry, record, tool_runner_factory)
    if not passed:
        why = f"a live example failed: {smoke[-1] if smoke else ''}"
        event = approvals.deny(registry, approval_id, record.id, by=approvals.FORGE, reason=why)
        written = approvals.write_operator_event(
            settings, ledger_factory, event, {"approval_id": approval_id, "live_smoke": list(smoke)}
        )
        return Decided(
            record.label,
            approval_id,
            "rejected_by_smoke",
            lifecycle.REJECTED,
            (),
            smoke,
            (written,) if written else (),
        )
    with registry.db.transaction():
        registry.decide_approval(approval_id, "approved", decided_by=OPERATOR)
        event = registry.transition(record.id, lifecycle.ACTIVE, actor=OPERATOR, reason=text)
    written_files = _write_ledger(
        settings, {**event, "approval_id": approval_id}, ledger_factory, what="approved and active"
    )
    replaced = tuple(f"{e['tool']}@{e['version']}" for e in event.get("related", []))
    return Decided(
        record.label, approval_id, "approved", lifecycle.ACTIVE, replaced, smoke, tuple(written_files)
    )


__all__ = [
    "Decided",
    "Moved",
    "PROMOTABLE",
    "Promoted",
    "ReleaseContext",
    "decide",
    "parse_label",
    "promote",
    "quarantine",
    "release",
    "release_context",
]
