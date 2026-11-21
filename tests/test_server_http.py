"""The server over its two transports: a real streamable HTTP listener behind `NetGuard`, and a real stdio subprocess.

Everything here uses loopback only. The listener runs in this process on a free port; nothing is exposed beyond the machine.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
import uvicorn
from mcp import Client, StdioServerParameters
from server_helpers import make_env

from toolforge.cli import main
from toolforge.errors import ConfigError
from toolforge.server.app import HTTP_PATH, build_http_app, describe_listener
from toolforge.server.tools import META_TOOLS

TOKEN = "t0ken-for-tests-only-0123456789abcdef"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
JSON_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Reply:
    def __init__(self, status_code: int, headers: dict[str, str], content: bytes) -> None:
        self.status_code, self.headers, self.content = status_code, headers, content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class Listener:
    def __init__(self, tmp_path: Path, **settings) -> None:
        self.port = free_port()
        self.env = make_env(tmp_path, server_port=self.port, **settings)
        self.url = f"http://127.0.0.1:{self.port}{HTTP_PATH}"
        asgi = build_http_app(self.env.app)
        config = uvicorn.Config(asgi, host="127.0.0.1", port=self.port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Listener:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), "the listener did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.env.registry.close()

    def post(self, headers: dict[str, str] | None = None, body: dict | None = None) -> Reply:
        merged = {**JSON_HEADERS, **(headers or {})}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(
                "POST", HTTP_PATH, json.dumps(body or INITIALIZE), merged
            )  # a `Host` in `merged` replaces the default
            response = connection.getresponse()
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
        finally:
            connection.close()


@pytest.fixture
def open_listener(tmp_path: Path) -> Iterator[Listener]:
    with Listener(tmp_path) as listener:
        yield listener


@pytest.fixture
def token_listener(tmp_path: Path) -> Iterator[Listener]:
    with Listener(tmp_path, server_token=TOKEN) as listener:
        yield listener


# --- Host, Origin and token ----------------------------------------------------------------------------------------------------------


def test_a_loopback_listener_answers_a_normal_client(open_listener: Listener) -> None:
    response = open_listener.post()
    assert response.status_code == 200 and "toolforge" in response.text


def test_the_tools_list_over_http_is_the_table(open_listener: Listener) -> None:
    async def work():
        async with Client(open_listener.url) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert anyio.run(work) == [m.name for m in META_TOOLS]


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "localhost.evil.example:1"])
def test_a_wrong_host_header_is_403_and_reaches_nothing(open_listener: Listener, host: str) -> None:
    response = open_listener.post({"Host": host})
    assert response.status_code == 403 and response.content == b""


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "https://localhost.evil.example"])
def test_a_foreign_origin_is_403(open_listener: Listener, origin: str) -> None:
    assert open_listener.post({"Origin": origin}).status_code == 403


def test_a_loopback_origin_is_accepted(open_listener: Listener) -> None:
    assert open_listener.post({"Origin": "http://localhost:5173"}).status_code == 200


def test_with_a_token_a_request_without_it_is_401(token_listener: Listener) -> None:
    for headers in [
        None,
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
    ]:
        response = token_listener.post(headers)
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer", headers
        assert TOKEN not in response.text


def test_with_a_token_the_right_header_is_accepted(token_listener: Listener) -> None:
    assert token_listener.post({"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_the_token_is_not_in_the_startup_line_or_the_settings_display(token_listener: Listener) -> None:
    line = describe_listener(token_listener.env.settings)
    assert TOKEN not in line and "bearer token required" in line
    assert TOKEN not in json.dumps(token_listener.env.settings.masked(), default=str)


# --- Refusing to start -------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "tools.example.com"])
def test_a_non_loopback_host_without_a_token_does_not_start(tmp_path: Path, host: str) -> None:
    environment = make_env(tmp_path, server_host=host)
    try:
        with pytest.raises(ConfigError, match="TOOLFORGE_SERVER_TOKEN"):
            build_http_app(environment.app)
    finally:
        environment.registry.close()


def test_a_blank_token_does_not_count(tmp_path: Path) -> None:
    environment = make_env(tmp_path, server_host="0.0.0.0", server_token="   ")
    try:
        with pytest.raises(ConfigError):
            build_http_app(environment.app)
    finally:
        environment.registry.close()


def test_a_non_loopback_host_with_a_token_builds(tmp_path: Path) -> None:
    environment = make_env(tmp_path, server_host="0.0.0.0", server_token=TOKEN)
    try:
        assert build_http_app(environment.app)
    finally:
        environment.registry.close()


def test_the_serve_command_refuses_before_it_listens(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["serve", "--transport", "http", "--host", "0.0.0.0", "--port", str(free_port())]) == 1
    err = capsys.readouterr().err
    assert "TOOLFORGE_SERVER_TOKEN" in err and "not a loopback address" in err


# --- stdio ---------------------------------------------------------------------------------------------------------------------------------


def test_stdio_carries_only_protocol_messages(tmp_home: Path) -> None:
    """A real `toolforge serve` subprocess: a client can list the tools, and nothing but JSON-RPC appears on stdout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOOLFORGE_")}
    env["TOOLFORGE_HOME"] = str(tmp_home)
    command = [sys.executable, "-m", "toolforge.cli", "serve"]

    async def work() -> list[str]:
        params = StdioServerParameters(command=command[0], args=command[1:], env=env)
        async with Client(params) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert anyio.run(work) == [m.name for m in META_TOOLS]


def test_stdio_stdout_is_clean_when_the_input_is_closed(tmp_home: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOOLFORGE_")}
    env["TOOLFORGE_HOME"] = str(tmp_home)
    request = json.dumps(INITIALIZE) + "\n"
    done = subprocess.run(
        [sys.executable, "-m", "toolforge.cli", "serve"],
        input=request,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert lines, done.stderr
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0", line  # a stray print would fail here
