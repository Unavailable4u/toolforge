"""`toolforge promote <name>@<version> --reason "..."`: the operator makes a verified version active.

This is an operator command on purpose. No MCP tool does it and the server process cannot reach it (`services/operator_actions.py`).
It is the normal path while no language model is configured, because gate G5 is then skipped and a version stops at `verified_basic`.
"""

from __future__ import annotations

import argparse
import sys

from toolforge import cli_run
from toolforge.config import load_settings
from toolforge.registry import Registry
from toolforge.services import operator_actions


def _cmd_promote(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        done = operator_actions.promote(
            settings, registry, args.ref, args.reason, ledger_factory=cli_run.make_ledger
        )
    print(done.text())
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    promote = subparsers.add_parser(
        "promote",
        help="make a verified tool version active, so agents can find and run it",
        description=(
            "Move a version in status verified_basic or verified to active and write forge.promoted, with your reason, to the "
            "ledger. Versions that ask for network access or secrets need approval and are refused. This command is for the operator; "
            "no agent tool can do it."
        ),
    )
    promote.add_argument(
        "ref", metavar="name@version", help="the version to promote, for example csv_summary@1.0.0"
    )
    promote.add_argument(
        "--reason", required=True, help="why you are letting this version in (recorded in the ledger)"
    )
    promote.set_defaults(handler=_cmd_promote)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
