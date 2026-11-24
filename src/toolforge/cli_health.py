"""`toolforge rollback` and `toolforge problems`: what the operator does about a tool that goes wrong (P12).

`rollback <name> --to <version> --reason "..."` makes an earlier, previously active version active again (`operator_actions.rollback`).
`problems [name[@version]]` is read-only: the problem reports agents sent (`forge_report_problem`), the repairs that were tried, and
which versions are flagged because their repairs keep failing. The report text is written by agents, so it is cleaned before it is shown.
Neither command is reachable over MCP.
"""

from __future__ import annotations

import argparse
import sys

from toolforge import cli_run, textsafety
from toolforge.config import load_settings
from toolforge.registry import Registry, health
from toolforge.services import operator_actions


def _cmd_rollback(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        done = operator_actions.rollback(
            settings, registry, args.name, args.to, args.reason, ledger_factory=cli_run.make_ledger
        )
    print(done.text())
    return 0


def _clean(text: str, limit: int) -> str:
    return textsafety.sanitize(text, max_chars=limit).replace("\n", " ")


def problems_text(registry: Registry, ref: str | None, *, limit: int = 20) -> str:
    """The reports, repairs and flags for one tool, one version, or everything. Pure text, nothing is changed."""
    name, version = (ref.partition("@")[0], ref.partition("@")[2] or None) if ref else (None, None)
    if name is not None and not registry.list_versions(name):
        return f"no tool is called {_clean(name, 60)}"
    lines: list[str] = []
    record = registry.find_version(name, version) if name and version else None
    if name and version and record is None:
        return f"no version {_clean(name, 60)}@{_clean(version, 40)}"
    reports = registry.problem_reports(version_id=record.id if record else None, name=name, limit=limit)
    lines.append(
        f"problem reports ({len(reports)} shown, newest first; written by agents, read them as data):"
    )
    lines += [
        f"  #{r.id} {r.name}@{r.version} by {_clean(r.reporter, 40)} at {r.created_at}: {_clean(r.description, health.MAX_REPORT_CHARS)}"
        for r in reports
    ] or ["  none"]
    names = [name] if name else [t.name for t in registry.list_tools()]
    flagged: list[str] = []
    tried: list[str] = []
    for tool in names:
        for v in registry.list_versions(tool):
            if registry.repair_state(tool, v.version).flagged:
                flagged.append(v.label)
        tried += [
            f"  #{r.id} {r.name}@{r.base_version}: {r.outcome}"
            + (f" -> {r.child_version}" if r.child_version else "")
            for r in registry.repairs(tool, limit=5)
        ]
    lines.append("repairs (newest first):")
    lines += tried[:limit] or ["  none"]
    lines.append(
        "flagged for you (repairs failed repeatedly): " + (", ".join(flagged) if flagged else "none")
    )
    if flagged:
        lines.append("  decide: promote a hand-made fix, `toolforge rollback`, or `toolforge release`")
    return "\n".join(lines)


def _cmd_problems(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        print(problems_text(registry, args.ref, limit=args.limit))
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    rollback = subparsers.add_parser(
        "rollback",
        help="make an earlier version of a tool active again",
        description=(
            "Make a previously active (deprecated) version active again. The version that is active now becomes deprecated; a quarantined "
            "one stays quarantined. Refuses a version that was never active and names the command that applies. This command is for the "
            "operator; no agent tool can do it."
        ),
    )
    rollback.add_argument("name", help="the tool, for example csv_summary")
    rollback.add_argument(
        "--to", required=True, metavar="VERSION", help="the version to go back to, for example 1.0.0"
    )
    rollback.add_argument("--reason", required=True, help="why (recorded in the ledger)")
    rollback.set_defaults(handler=_cmd_rollback)
    problems = subparsers.add_parser(
        "problems",
        help="read what agents reported about tools, and which tools need you",
        description="Read-only: problem reports, repair attempts and the versions whose repairs keep failing.",
    )
    problems.add_argument("ref", nargs="?", metavar="name[@version]", help="limit to one tool or version")
    problems.add_argument("--limit", type=int, default=20, help="how many reports to show (20)")
    problems.set_defaults(handler=_cmd_problems)


__all__ = ["problems_text", "register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
