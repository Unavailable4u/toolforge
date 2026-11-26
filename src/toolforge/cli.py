"""Command line entry point.

Later patches add subcommands by writing a `register(subparsers)` function in their own module and appending it to
`REGISTRARS`, so no patch has to edit one giant function.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence

from toolforge import __version__
from toolforge import cli_approvals as approvals_commands
from toolforge import cli_attacks as attacks_commands
from toolforge import cli_build as build_commands
from toolforge import cli_dashboard as dashboard_commands
from toolforge import cli_doctor as doctor_commands
from toolforge import cli_health as health_commands
from toolforge import cli_images as images_commands
from toolforge import cli_package as package_commands
from toolforge import cli_promote as promote_commands
from toolforge import cli_provenance as provenance_commands
from toolforge import cli_registry as registry_commands
from toolforge import cli_release as release_commands
from toolforge import cli_report as report_commands
from toolforge import cli_run as run_commands
from toolforge import cli_serve as serve_commands
from toolforge import cli_sessions as sessions_commands
from toolforge import cli_verify as verify_commands
from toolforge.config import load_settings
from toolforge.errors import ToolForgeError

Handler = Callable[[argparse.Namespace], int]


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"toolforge {__version__}")
    return 0


def _cmd_config(_: argparse.Namespace) -> int:
    print(json.dumps(load_settings().masked(), indent=2, sort_keys=True, default=str))
    return 0


def _register_core(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser("version", help="print the version").set_defaults(handler=_cmd_version)
    subparsers.add_parser("config", help="print effective settings, secrets masked").set_defaults(
        handler=_cmd_config
    )


REGISTRARS: list[Callable[[argparse._SubParsersAction], None]] = [
    _register_core,
    approvals_commands.register,
    attacks_commands.register,
    build_commands.register,
    dashboard_commands.register,
    doctor_commands.register,
    health_commands.register,
    images_commands.register,
    package_commands.register,
    promote_commands.register,
    provenance_commands.register,
    registry_commands.register,
    release_commands.register,
    report_commands.register,
    run_commands.register,
    serve_commands.register,
    sessions_commands.register,
    verify_commands.register,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolforge", description="Build, verify and reuse agent tools, safely."
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for register in REGISTRARS:
        register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except ToolForgeError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
