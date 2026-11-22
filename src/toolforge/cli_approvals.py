"""`toolforge approvals list|show|approve|deny`: the operator's side of gate G7.

A version that asks for network hosts or secrets waits as `pending_approval` until a person answers here. These commands are the only
way to answer: no MCP tool can approve or deny (`services/operator_actions.decide`, which the server never imports). `show` prints the
request (the permission diff, the gates that did not run, the Blast Radius summary, the limits of the egress proxy). `approve` and
`deny` print it again and then act: the command and its required `--reason` are the decision, as with `release`.

Every command first denies requests that are overdue (`TOOLFORGE_APPROVAL_TIMEOUT_S`), because nothing else runs a timer.
Approving runs the version's `live` examples once, so it needs Docker only when there are any.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from toolforge import cli_run
from toolforge.config import load_settings
from toolforge.errors import RegistryError
from toolforge.registry import APPROVAL_STATUSES, Registry
from toolforge.services import approvals, operator_actions
from toolforge.services.runner import ToolRunner

SHOWN = (*APPROVAL_STATUSES, "all")


def _approval(registry: Registry, approval_id: int) -> Any:
    found = registry.get_approval(approval_id)
    if found is None:
        raise RegistryError(
            f"no approval request with id {approval_id}",
            code="unknown_approval",
            hint="list them with `toolforge approvals list --status all`",
        )
    return found


def _cmd_list(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        for gone in approvals.expire_overdue(settings, registry, ledger_factory=cli_run.make_ledger):
            print(f"approval {gone.approval_id} ({gone.label}) timed out and was denied")
        rows = registry.list_approvals(None if args.status == "all" else args.status)
        if not rows:
            print(f"no {'' if args.status == 'all' else args.status + ' '}approval requests")
            return 0
        for row in rows:
            doc = row.requested if isinstance(row.requested, dict) else {}
            asked = doc.get("requested") or {}
            weaker = f" [{','.join(doc['skipped_gates'])} not run]" if doc.get("skipped_gates") else ""
            print(
                f"{row.id}\t{row.status}\t{doc.get('tool', '?')}\t"
                f"network: {','.join(asked.get('network') or []) or '-'}\t"
                f"secrets: {','.join(asked.get('secrets') or []) or '-'}\texpires {doc.get('expires_at')}{weaker}"
            )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        approvals.expire_overdue(settings, registry, ledger_factory=cli_run.make_ledger)
        row = _approval(registry, args.id)
        record = registry.get_version(row.version_id)
        print(approvals.render_text(row, record.status if record else None))
    return 0


def _decide(args: argparse.Namespace, decision: str) -> int:
    settings = load_settings()
    sandboxes: list[object] = []

    def make_tool_runner() -> ToolRunner:
        sandbox = cli_run.make_sandbox_runner(settings)
        sandboxes.append(sandbox)
        return ToolRunner(sandbox, registry, settings, cli_run.make_ledger)

    try:
        with Registry.from_settings(settings) as registry:
            approvals.expire_overdue(settings, registry, ledger_factory=cli_run.make_ledger)
            row = _approval(registry, args.id)
            record = registry.get_version(row.version_id)
            print(approvals.render_text(row, record.status if record else None))
            print("-" * 40)
            done = operator_actions.decide(
                settings,
                registry,
                args.id,
                decision,
                args.reason,
                tool_runner_factory=make_tool_runner,
                ledger_factory=cli_run.make_ledger,
            )
    finally:
        for sandbox in sandboxes:
            stop = getattr(sandbox, "shutdown", None)
            if stop is not None:
                stop()
    print(done.text())
    return 1 if done.outcome == "rejected_by_smoke" else 0


def _cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, "approve")


def _cmd_deny(args: argparse.Namespace) -> int:
    return _decide(args, "deny")


def register(subparsers: argparse._SubParsersAction) -> None:
    root = subparsers.add_parser(
        "approvals",
        help="answer approval requests for tools that ask for network hosts or secrets",
        description=(
            "A tool version whose manifest asks for network hosts or secrets waits as pending_approval until you answer here. "
            "For the operator only; no agent tool can approve or deny."
        ),
    )
    sub = root.add_subparsers(dest="approvals_command", metavar="<action>", required=True)

    listing = sub.add_parser("list", help="list approval requests (pending ones by default)")
    listing.add_argument("--status", choices=SHOWN, default="pending", help="which requests to list")
    listing.set_defaults(handler=_cmd_list)

    show = sub.add_parser("show", help="print one request: permission diff, gates, Blast Radius, limits")
    show.add_argument("id", type=int, help="the approval id from `approvals list`")
    show.set_defaults(handler=_cmd_show)

    approve = sub.add_parser(
        "approve",
        help="approve a request: the version becomes active (live examples run first)",
        description=(
            "Print the request, run the version's live examples once with the approved network, then make it active and write "
            "forge.approval.granted, with your reason, to its operator ledger. A failing live example rejects the version instead."
        ),
    )
    approve.add_argument("id", type=int, help="the approval id")
    approve.add_argument(
        "--reason", required=True, help="why these permissions are acceptable (recorded in the ledger)"
    )
    approve.set_defaults(handler=_cmd_approve)

    deny = sub.add_parser("deny", help="deny a request: the version is rejected")
    deny.add_argument("id", type=int, help="the approval id")
    deny.add_argument("--reason", required=True, help="why (recorded in the ledger)")
    deny.set_defaults(handler=_cmd_deny)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
