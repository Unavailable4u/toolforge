"""`toolforge dashboard`: the operator dashboard, a web page for the person who runs ToolForge.

    toolforge dashboard [--host H] [--port P] [--runs-dir DIR]

It binds `TOOLFORGE_DASHBOARD_HOST:PORT` (127.0.0.1:8787 by default). A host that is not a loopback address needs
`TOOLFORGE_DASHBOARD_TOKEN`, or the command refuses to start; with a token, everything under `/api` must carry
`Authorization: Bearer <token>`. `--host` and `--port` override the settings; the token is only ever read from the environment, never
from the command line. `--runs-dir` is where `toolforge attacks` and `toolforge bench` wrote their results (default `runs`, in the
directory this command is started from). It is a separate process from `toolforge serve`; both read the same registry.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import anyio

from toolforge.config import load_settings
from toolforge.dashboard import app as dashboard_app


def _logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _cmd_dashboard(args: argparse.Namespace) -> int:
    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["dashboard_host"] = args.host
    if args.port is not None:
        overrides["dashboard_port"] = args.port
    settings = load_settings(**overrides)
    _logging(settings.log_level)
    # Building the app refuses a non-loopback host without a token, before anything listens.
    dash = dashboard_app.build_dashboard(settings, runs_dir=Path(args.runs_dir))
    try:
        print(dashboard_app.describe_listener(settings), file=sys.stderr)
        anyio.run(dashboard_app.serve, dash)
    except KeyboardInterrupt:
        pass
    finally:
        dash.close()
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    dash = subparsers.add_parser(
        "dashboard",
        help="run the operator dashboard (a local web page)",
        description=(
            "Serve the operator dashboard on TOOLFORGE_DASHBOARD_HOST:TOOLFORGE_DASHBOARD_PORT (127.0.0.1:8787 by default). "
            "It needs TOOLFORGE_DASHBOARD_TOKEN on any address that is not loopback."
        ),
    )
    dash.add_argument(
        "--host", default=None, help="address to bind (default: TOOLFORGE_DASHBOARD_HOST, 127.0.0.1)"
    )
    dash.add_argument(
        "--port", type=int, default=None, help="port to bind (default: TOOLFORGE_DASHBOARD_PORT, 8787)"
    )
    dash.add_argument(
        "--runs-dir",
        default="runs",
        help="where `toolforge attacks` and `toolforge bench` wrote their results (default: ./runs)",
    )
    dash.set_defaults(handler=_cmd_dashboard)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
