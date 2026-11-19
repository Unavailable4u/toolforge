"""`toolforge registry list` and `toolforge registry show`: read-only views of the registry.

Neither command changes anything or runs tool code. Text that came from a tool author (description, when_to_use) is shown with
control characters replaced, so a hostile manifest cannot move the operator's cursor or hide text. A missing measurement is shown
as "not measured", never as 0.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from toolforge.config import load_settings
from toolforge.errors import ToolForgeError
from toolforge.registry import Registry, VersionRecord
from toolforge.registry.db import STATS_MIN_CALLS, Stats

_LIST_HINT = "see what exists with `toolforge registry list`"


def _plain(value: object, limit: int = 300) -> str:
    text = str(value)
    return "".join(char if char.isprintable() else "?" for char in text)[:limit]


def _rate(stats: Stats) -> str:
    if stats.success_rate is None:
        return "not measured"
    return f"{stats.success_rate * 100:.0f}% of last {stats.recent_calls}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    lines = ["  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    lines += ["  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip() for row in rows]
    return "\n".join(lines)


def _version_json(registry: Registry, record: VersionRecord) -> dict[str, Any]:
    stats = registry.stats(record.id)
    return {
        "id": record.id,
        "name": record.name,
        "version": record.version,
        "status": record.status,
        "code_sha": record.code_sha,
        "blob_sha": record.blob_sha,
        "created_by": record.created_by,
        "parent_version": record.parent_version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "uses": stats.uses,
        "success_rate": stats.success_rate,  # null means not measured
    }


def _versions_table(registry: Registry, records: Sequence[VersionRecord]) -> str:
    rows = []
    for record in records:
        stats = registry.stats(record.id)
        rows.append(
            [
                _plain(record.name),
                _plain(record.version),
                record.status,
                str(stats.uses),
                _rate(stats),
                _plain(record.created_at),
            ]
        )
    return _table(["NAME", "VERSION", "STATUS", "USES", "SUCCESS RATE", "CREATED"], rows)


def _cmd_list(args: argparse.Namespace) -> int:
    with Registry.from_settings(load_settings()) as registry:
        records = registry.list_versions()
        if args.json:
            print(json.dumps({"versions": [_version_json(registry, r) for r in records]}, indent=2))
            return 0
        print(_versions_table(registry, records))
        print(f"\n{len(records)} version{'s' if len(records) != 1 else ''}")
    return 0


def _split_target(target: str) -> tuple[str, str | None]:
    name, at, version = target.partition("@")
    if not name or (at and not version):
        raise ToolForgeError(
            f"{_plain(target, 80)!r} is not a tool name or name@version",
            code="invalid_target",
            hint="write it as word_count or word_count@1.0.0",
        )
    return name, (version if at else None)


def _cmd_show(args: argparse.Namespace) -> int:
    name, version = _split_target(args.target)
    with Registry.from_settings(load_settings()) as registry:
        if version is None:
            return _show_tool(registry, name, as_json=args.json)
        record = registry.find_version(name, version)
        if record is None:
            raise ToolForgeError(
                f"no version {_plain(name, 60)}@{_plain(version, 40)}",
                code="unknown_version",
                hint=_LIST_HINT,
            )
        return _show_version(registry, record, as_json=args.json)


def _show_tool(registry: Registry, name: str, *, as_json: bool) -> int:
    tool = registry.get_tool(name)
    if tool is None:
        raise ToolForgeError(f"no tool called {_plain(name, 60)!r}", code="unknown_tool", hint=_LIST_HINT)
    records = registry.list_versions(name=name)
    if as_json:
        payload = {
            "name": tool.name,
            "active_version": tool.active_version,
            "created_at": tool.created_at,
            "versions": [_version_json(registry, r) for r in records],
        }
        print(json.dumps(payload, indent=2))
        return 0
    print(f"tool: {_plain(tool.name)}")
    print(f"active version: {_plain(tool.active_version) if tool.active_version else 'none'}")
    print(f"registered: {_plain(tool.created_at)}\n")
    print(_versions_table(registry, records))
    print(f"\ndetails of one version: toolforge registry show {_plain(tool.name)}@<version>")
    return 0


def _permissions_line(record: VersionRecord) -> str:
    permissions = record.permissions
    network = ", ".join(_plain(h, 80) for h in permissions.get("network", [])) or "none"
    secrets = ", ".join(_plain(s, 40) for s in permissions.get("secrets", [])) or "none"
    return f"network: {network}; secrets: {secrets}; caller files: {'yes' if permissions.get('inputs') else 'no'}"


def _show_version(registry: Registry, record: VersionRecord, *, as_json: bool) -> int:
    gates = registry.gate_results(record.id)
    approvals = [a for a in registry.list_approvals() if a.version_id == record.id]
    if as_json:
        payload = _version_json(registry, record)
        payload["manifest"] = record.manifest
        payload["gates"] = [
            {
                "gate": g.gate,
                "status": g.status,
                "detail": g.detail,
                "started_at": g.started_at,
                "finished_at": g.finished_at,
            }
            for g in gates
        ]
        payload["approvals"] = [
            {
                "id": a.id,
                "status": a.status,
                "requested": a.requested,
                "decided_by": a.decided_by,
                "decided_at": a.decided_at,
            }
            for a in approvals
        ]
        print(json.dumps(payload, indent=2))
        return 0
    stats = registry.stats(record.id)
    manifest = record.manifest
    print(f"{_plain(record.name)} {_plain(record.version)}  [{record.status}]")
    print(f"  description:  {_plain(manifest.get('description', ''), 600)}")
    print(f"  when to use:  {_plain(manifest.get('when_to_use', '')) or '(not given)'}")
    print(f"  permissions:  {_permissions_line(record)}")
    print(f"  code sha:     {record.code_sha}")
    print(f"  blob sha:     {record.blob_sha or 'not frozen yet'}")
    print(f"  parent:       {_plain(record.parent_version) if record.parent_version else 'none'}")
    print(
        f"  created:      {_plain(record.created_at)} by {_plain(record.created_by)}; updated {_plain(record.updated_at)}"
    )
    note = f" (needs {STATS_MIN_CALLS} calls)" if stats.success_rate is None else ""
    print(f"  calls:        {stats.uses} used; success rate {_rate(stats)}{note}")
    print("\ngates:")
    if gates:
        print(
            _table(
                ["GATE", "STATUS", "FINISHED"],
                [[_plain(g.gate, 20), g.status, _plain(g.finished_at)] for g in gates],
            )
        )
    else:
        print("  none recorded yet")
    if approvals:
        print("\napprovals:")
        print(
            _table(
                ["ID", "STATUS", "DECIDED BY"],
                [[str(a.id), a.status, _plain(a.decided_by or "-", 80)] for a in approvals],
            )
        )
    return 0


def _cmd_help(args: argparse.Namespace) -> int:
    args.registry_parser.print_help()
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    registry = subparsers.add_parser(
        "registry",
        help="look at registered tools and their versions",
        description="Read-only views of the registry in $TOOLFORGE_HOME.",
    )
    registry.set_defaults(handler=_cmd_help, registry_parser=registry)
    commands = registry.add_subparsers(dest="registry_command", metavar="<command>")

    listing = commands.add_parser("list", help="list every version with its status and call statistics")
    listing.add_argument("--json", action="store_true", help="print JSON instead of a table")
    listing.set_defaults(handler=_cmd_list)

    show = commands.add_parser(
        "show",
        help="show one tool (its versions) or one version (manifest summary, gates, approvals, statistics)",
        description="Show a tool by name, or one version as name@version.",
    )
    show.add_argument("target", help="a tool name such as word_count, or a version such as word_count@1.0.0")
    show.add_argument("--json", action="store_true", help="print JSON instead of text")
    show.set_defaults(handler=_cmd_show)


__all__ = ["register"]
