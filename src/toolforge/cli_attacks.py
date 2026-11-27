"""`toolforge attacks list` and `toolforge attacks run` (P15): the hostile-author catalogue against the real stack.

`list` prints the catalogue (`--markdown` prints the table that `docs/ATTACKS.md` embeds). `run` drives the attacks one after another, each in a
throwaway data directory, prints a table of verdicts and a summary, and with `--json` also writes a machine-readable file under
`runs/attacks/`. Attacks that need Docker run only when a daemon answers and the runtime image is built; otherwise they are `skipped`.

The exit code is 1 when any attack was `breached`, 2 when none was but something is `inconclusive` or `skipped`, and 0 when every attack
that ran was `contained`. The command never opens a sandbox for anything but the attacks, and never contacts a real third party.
"""

from __future__ import annotations

import argparse
import sys

from toolforge.attacks import catalog, runner
from toolforge.config import load_settings


def _cmd_list(args: argparse.Namespace) -> int:
    if args.markdown:
        print(catalog.attacks_markdown())
        return 0
    width = max(len(a.id) for a in catalog.ATTACKS)
    for attack in catalog.ATTACKS:
        needs = "docker" if attack.needs_docker else "no docker"
        print(f"{attack.id:<{width}}  {needs:<9}  {attack.title}")
    print(f"\n{len(catalog.ATTACKS)} attacks. `toolforge attacks run [--only <id>]` runs them.")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    def progress(attack: catalog.Attack, result: runner.AttackResult | None) -> None:
        if result is None:
            print(f"running {attack.id} ...", file=sys.stderr, flush=True)
        else:
            print(f"  {result.verdict} ({result.seconds:.0f} s)", file=sys.stderr, flush=True)

    results = runner.run_attacks(args.only, deadline_s=args.deadline, progress=progress)
    print(runner.render_table(results))
    if args.json:
        path = runner.write_json(results, runner.RUNS_DIR, docker=runner.docker_problem(load_settings()))
        print(f"\nwrote {path}")
    return runner.exit_code(results)


def register(subparsers: argparse._SubParsersAction) -> None:
    attacks = subparsers.add_parser(
        "attacks",
        help="run the hostile-author attack catalogue against the real stack",
        description=(
            "Attacks build hostile tool packages and drive them through the real pipeline, runtime, server and ledger. Each ends as "
            "contained, breached, inconclusive or skipped, judged from the ledger, the registry and Docker's own report. "
            "A passing suite is a statement about this catalogue, not about all possible attacks."
        ),
    )
    sub = attacks.add_subparsers(dest="attacks_command", metavar="<list|run>", required=True)
    listing = sub.add_parser("list", help="list the attacks")
    listing.add_argument(
        "--markdown", action="store_true", help="print the table that docs/ATTACKS.md embeds"
    )
    listing.set_defaults(handler=_cmd_list)
    run = sub.add_parser("run", help="run the attacks and print a verdict for each")
    run.add_argument("--only", action="append", metavar="ID", help="run only this attack (repeatable)")
    run.add_argument("--json", action="store_true", help="also write the results under runs/attacks/")
    run.add_argument(
        "--deadline",
        type=float,
        default=runner.DEADLINE_S,
        metavar="SECONDS",
        help=f"hard limit per attack (default {runner.DEADLINE_S})",
    )
    run.set_defaults(handler=_cmd_run)


__all__ = ["register"]
