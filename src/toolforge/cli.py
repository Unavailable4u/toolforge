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
from toolforge import cli_doctor as doctor_commands
from toolforge import cli_package as package_commands
from toolforge import cli_registry as registry_commands
from toolforge import cli_run as run_commands
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
    doctor_commands.register,
    package_commands.register,
    registry_commands.register,
    run_commands.register,
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
