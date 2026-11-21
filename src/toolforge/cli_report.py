"""`toolforge report <name>@<version> [--json | --html]`: the Blast Radius of a tool version.

The report is built again from the version's ledgers every time (the verification run's G6 window and a sample of real calls),
stored as `$TOOLFORGE_HOME/ledgers/<name>@<version>/blast-radius.json`, and printed. It needs no Docker. A report says what was
measured and what was not; a clean one is evidence of what a tool did on the runs that were watched, not proof of what it can do.
`--html` prints one self-contained page (redirect it to a file); `--json` prints the stored document.
"""

from __future__ import annotations

import argparse
import json
import sys

from toolforge.config import load_settings
from toolforge.errors import RegistryError
from toolforge.registry import Registry
from toolforge.services import operator_actions


def _cmd_report(args: argparse.Namespace) -> int:
    from toolforge.report import blast_radius

    settings = load_settings()
    name, version = operator_actions.parse_label(args.ref)
    with Registry.from_settings(settings) as registry:
        record = registry.find_version(name, version)  # the registry vets the name before it becomes a path
    if record is None:
        raise RegistryError(
            f"no version {name}@{version}",
            code="unknown_version",
            hint="list what exists with `toolforge registry list`",
        )
    document = blast_radius.generate(settings, record.name, record.version, store=not args.no_store)
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True))
    elif args.html:
        print(blast_radius.render_html(document))
    else:
        print(blast_radius.render_text(document))
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    report = subparsers.add_parser(
        "report",
        help="show the Blast Radius of a tool version: what it touched while it was watched",
        description=(
            "Build the Blast Radius report of a version from its ledgers, store it next to them and print it. Anything that was "
            "not measured is said so; it is never shown as zero."
        ),
    )
    report.add_argument("ref", metavar="name@version", help="the version, for example csv_summary@1.0.0")
    shape = report.add_mutually_exclusive_group()
    shape.add_argument("--json", action="store_true", help="print the report as JSON")
    shape.add_argument("--html", action="store_true", help="print the report as one self-contained HTML page")
    report.add_argument("--no-store", action="store_true", help="do not write blast-radius.json")
    report.set_defaults(handler=_cmd_report)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
