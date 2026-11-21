"""`toolforge serve`: run the MCP server over stdio (default) or streamable HTTP.

    toolforge serve                                   # stdio: an MCP client starts this command and talks over its pipes
    toolforge serve --transport http [--host H] [--port P]

stdio exposes nothing to the network, and stdout carries only protocol messages: this command never prints to it. Logs go to stderr.
HTTP binds `TOOLFORGE_SERVER_HOST:PORT` (127.0.0.1:8765 by default). A host that is not a loopback address needs
`TOOLFORGE_SERVER_TOKEN`, or the server refuses to start; with a token, every request must carry `Authorization: Bearer <token>`.
`--host` and `--port` override the settings; the token is only ever read from the environment, never from the command line.
"""

from __future__ import annotations

import argparse
import logging
import sys

import anyio

from toolforge.config import load_settings
from toolforge.server import app as server_app

TRANSPORTS = ("stdio", "http")


def _logging(level: str) -> None:
    """All logging to stderr (stdout belongs to the protocol in stdio mode)."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["server_host"] = args.host
    if args.port is not None:
        overrides["server_port"] = args.port
    settings = load_settings(**overrides)
    _logging(settings.log_level)
    app = server_app.build_server(settings)
    try:
        if args.transport == "http":
            server_app.build_http_app(
                app
            )  # refuses a non-loopback host without a token, before anything listens
            print(server_app.describe_listener(settings), file=sys.stderr)
            anyio.run(server_app.serve_http, app)
        else:
            anyio.run(server_app.serve_stdio, app)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    serve = subparsers.add_parser(
        "serve",
        help="run the MCP server (stdio by default, or streamable HTTP)",
        description=(
            "Serve ToolForge's tools to an MCP client. stdio is for a client that starts this command; http listens on "
            "TOOLFORGE_SERVER_HOST:TOOLFORGE_SERVER_PORT and needs TOOLFORGE_SERVER_TOKEN on any address that is not loopback."
        ),
    )
    serve.add_argument("--transport", choices=TRANSPORTS, default="stdio", help="stdio (default) or http")
    serve.add_argument(
        "--host", default=None, help="http only: address to bind (default: TOOLFORGE_SERVER_HOST, 127.0.0.1)"
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="http only: port to bind (default: TOOLFORGE_SERVER_PORT, 8765)",
    )
    serve.set_defaults(handler=_cmd_serve)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
