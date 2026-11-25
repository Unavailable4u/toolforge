"""`toolforge sessions list` and `toolforge sessions close` (P13): what the operator sees and does about stateful sessions.

A session's sandbox belongs to the server process that opened it, so these commands work through the registry's session table:

* `list` is read-only. It shows the public name of each session (never the secret half of an id), the tool version, the step count and
  how long it has left. `--all` includes sessions that ended, with the reason.
* `close <session>` asks the owning server to close the session (it audits and destroys the sandbox, and writes the Blast Radius report)
  and waits a few seconds for that. If the server that owns it is gone (it stopped reporting), the container is removed here and the
  session is marked as ended by the operator; no audit can happen then. `close --all` does this for every open session.

Neither command is reachable over MCP: a caller ends only its own session, with `forge_session_close` and the id it holds.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from toolforge import cli_doctor
from toolforge.config import load_settings
from toolforge.errors import ToolForgeError
from toolforge.registry import Registry, sessions

WAIT_S = 20.0
POLL_S = 0.5


def _public(value: str) -> str:
    """Accepts a public name or a full session id; only the public half is ever used here."""
    return value.partition(".")[0]


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s"


def render_list(rows: list[sessions.SessionRow], now: float) -> str:
    if not rows:
        return "no sessions"
    lines = [
        "session      state        tool                          steps  age      idle     left     owner"
    ]
    for r in rows:
        if r.is_open:
            owner = "gone" if r.orphaned(now) else "running"
            left = _age(min(r.expires_at, r.hard_expires_at) - now)
        else:
            owner, left = "-", "-"
        state = r.state if r.is_open else f"{r.state}"
        lines.append(
            f"{r.public_id:<12} {state:<12} {r.label[:29]:<29} {r.steps:<6} {_age(now - r.opened_at):<8} "
            f"{_age(now - r.last_used_at):<8} {left:<8} {owner}"
            + ("" if r.is_open or not r.reason else f"  ({r.reason[:60]})")
        )
    return "\n".join(lines)


def _cmd_list(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        rows = registry.list_sessions(state=None if args.all else "open", limit=args.limit)
    print(render_list(rows, time.time()))
    return 0


def _remove_container(settings: Any, name: str | None) -> bool:
    """Remove the container of a session whose server is gone. False when it could not be (Docker unreachable, already gone is fine)."""
    if not name:
        return True
    env = cli_doctor.DoctorEnv(settings)
    try:
        client = env.client()
        try:
            client.containers.get(name).remove(force=True)
        except Exception as exc:
            if type(exc).__name__ != "NotFound":
                return False
        return True
    except ToolForgeError:
        return False


def _close_one(settings: Any, registry: Registry, public: str, *, wait_s: float = WAIT_S) -> tuple[bool, str]:
    row = registry.get_session(public)
    if row is None:
        return False, f"no session {public}"
    if not row.is_open:
        return True, f"{public} was already closed ({row.state}: {row.reason or 'no reason'})"
    now = time.time()
    if row.orphaned(now):
        if not _remove_container(settings, row.box):
            return (
                False,
                f"{public}: its server is gone and its container {row.box} could not be removed (try `docker rm -f {row.box}`)",
            )
        registry.close_session(
            public, "operator", "closed by the operator; its server was gone, so there was no audit", now=now
        )
        return True, f"{public} closed; its server was gone, so the container was removed without an audit"
    registry.request_session_close(public)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        row = registry.get_session(public)
        if row is not None and not row.is_open:
            return True, f"{public} closed ({row.state}: {row.reason or 'no reason'}); the server audited it"
        time.sleep(POLL_S)
    return (
        False,
        f"{public}: the close was requested, but the server has not closed it yet; run this again, or `toolforge doctor --clean`",
    )


def _cmd_close(args: argparse.Namespace) -> int:
    if args.all == bool(args.session):
        print("error [bad_arguments]: name one session, or use --all", file=sys.stderr)
        return 2
    settings = load_settings()
    ok = True
    with Registry.from_settings(settings) as registry:
        targets = (
            [r.public_id for r in registry.list_sessions(state="open", limit=1000)]
            if args.all
            else [_public(args.session)]
        )
        if not targets:
            print("no open sessions")
        for public in targets:
            done, text = _close_one(settings, registry, public)
            print(text)
            ok = ok and done
    return 0 if ok else 1


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sessions",
        help="list or close stateful sessions",
        description="Stateful sessions (tools with kind session) and what the operator can do about them.",
    )
    sub = parser.add_subparsers(dest="sessions_command", metavar="<action>", required=True)
    listing = sub.add_parser("list", help="show open sessions (read-only)")
    listing.add_argument("--all", action="store_true", help="include sessions that ended")
    listing.add_argument("--limit", type=int, default=50, help="at most this many (default 50)")
    listing.set_defaults(handler=_cmd_list)
    closing = sub.add_parser("close", help="close a session, or all of them")
    closing.add_argument("session", nargs="?", help="a session name such as s-1a2b3c4d")
    closing.add_argument("--all", action="store_true", help="close every open session")
    closing.set_defaults(handler=_cmd_close)


__all__ = ["register", "render_list"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
