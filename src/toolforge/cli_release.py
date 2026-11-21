"""`toolforge release` and `toolforge quarantine`: the operator pulls a tool version out of use, or lets a quarantined one back in.

Both are operator commands on purpose: no MCP tool does either and the server process cannot reach them
(`services/operator_actions.py`). A runtime decoy touch quarantines a version by itself; only a person releases it.
`release` prints why the version was quarantined and its Blast Radius summary first, then acts. It does not ask for confirmation,
because the command and its required `--reason` are the decision; read the output before you run it a second time.
"""

from __future__ import annotations

import argparse
import sys

from toolforge import cli_run
from toolforge.config import load_settings
from toolforge.registry import Registry
from toolforge.services import operator_actions


def _cmd_release(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        context = operator_actions.release_context(settings, registry, args.ref)
        print(context.text())
        done = operator_actions.release(
            settings, registry, args.ref, args.reason, ledger_factory=cli_run.make_ledger
        )
    print(done.text())
    return 0


def _cmd_quarantine(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        done = operator_actions.quarantine(
            settings, registry, args.ref, args.reason, ledger_factory=cli_run.make_ledger
        )
    print(done.text())
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    release = subparsers.add_parser(
        "release",
        help="put a quarantined tool version back in use",
        description=(
            "Print why a version was quarantined and its Blast Radius summary, then move it from quarantined back to active and "
            "write forge.released, with your reason, to its operator ledger. If another version of the tool is active now it is "
            "deprecated. For the operator only; no agent tool can do this."
        ),
    )
    release.add_argument(
        "ref", metavar="name@version", help="the quarantined version, for example csv_summary@1.0.0"
    )
    release.add_argument("--reason", required=True, help="why it is safe to release (recorded in the ledger)")
    release.set_defaults(handler=_cmd_release)

    quarantine = subparsers.add_parser(
        "quarantine",
        help="take an active tool version out of use",
        description=(
            "Move an active version to quarantined and write forge.quarantined, with your reason, to its operator ledger. It can "
            "no longer be found or run until you release it."
        ),
    )
    quarantine.add_argument(
        "ref", metavar="name@version", help="the active version, for example csv_summary@1.0.0"
    )
    quarantine.add_argument(
        "--reason", required=True, help="why you are stopping it (recorded in the ledger)"
    )
    quarantine.set_defaults(handler=_cmd_quarantine)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
