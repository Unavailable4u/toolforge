"""`NetGuard` and its parts: loopback detection, the bearer token, `Host` and `Origin` checks, and that the token never leaks."""

from __future__ import annotations

import logging

import anyio
import pytest
from pydantic import SecretStr

from toolforge.errors import ConfigError
from toolforge.netguard import (
    NetGuard,
    bearer_matches,
    host_header_values,
    is_loopback,
    origin_allowed,
    require_token,
    token_text,
)

TOKEN = "s3cret-token-value-1234567890"


def headers(host: str | None = "127.0.0.1:8765", **extra: str | list[str]) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    if host is not None:
        out.append((b"host", host.encode()))
    for key, value in extra.items():
        for item in [value] if isinstance(value, str) else value:
            out.append((key.replace("_", "-").encode(), item.encode()))
    return out


def guard(**options) -> NetGuard:
    async def app(scope, receive, send):  # pragma: no cover - never reached by evaluate()
        raise AssertionError

    options.setdefault("bind_host", "127.0.0.1")
    options.setdefault("port", 8765)
    return NetGuard(app, **options)


# --- Loopback and the token requirement -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.9.9.9", "::1", "[::1]", "localhost", "LocalHost.", "::ffff:127.0.0.1"]
)
def test_loopback_addresses(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "",
        "10.0.0.5",
        "192.168.1.2",
        "example.com",
        "localhost.evil.com",
        "127.0.0.1.evil.com",
        "0",
    ],
)
def test_everything_else_is_not_loopback(host: str) -> None:
    assert not is_loopback(host)


@pytest.mark.parametrize("token", [None, "", "   ", "\n", 5])
def test_a_blank_token_counts_as_no_token(token) -> None:
    assert token_text(token) is None
    with pytest.raises(ConfigError, match="TOOLFORGE_SERVER_TOKEN"):
        require_token("0.0.0.0", token)


def test_a_secret_str_token_is_read_and_loopback_needs_none() -> None:
    assert token_text(SecretStr(f"  {TOKEN} ")) == TOKEN
    require_token("127.0.0.1", None)
    require_token("0.0.0.0", SecretStr(TOKEN))


def test_the_refusal_names_the_variable_and_never_a_value() -> None:
    with pytest.raises(ConfigError) as caught:
        require_token("0.0.0.0", SecretStr("   "))
    assert "TOOLFORGE_SERVER_TOKEN" in str(caught.value) and "0.0.0.0" in str(caught.value)
    with pytest.raises(ConfigError) as caught:
        require_token("", None)
    assert "all interfaces" in str(caught.value)


def test_a_guard_on_a_public_address_without_a_token_cannot_be_built() -> None:
    with pytest.raises(ConfigError):
        guard(bind_host="0.0.0.0")
    with pytest.raises(ConfigError):
        guard(bind_host="192.168.1.5", token="  ")
    assert guard(bind_host="0.0.0.0", token=TOKEN)


# --- The token ------------------------------------------------------------------------------------------------------------------


def test_bearer_comparison() -> None:
    assert bearer_matches(f"Bearer {TOKEN}", TOKEN)
    assert bearer_matches(f"bearer   {TOKEN}  ", TOKEN)
    for bad in [
        None,
        "",
        TOKEN,
        f"Basic {TOKEN}",
        "Bearer ",
        f"Bearer {TOKEN}x",
        f"Bearer x{TOKEN}",
        "Bearer",
    ]:
        assert not bearer_matches(bad, TOKEN)
    assert not bearer_matches(f"Bearer {TOKEN}", "")


def test_bearer_check_uses_a_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    import hmac

    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("toolforge.netguard.hmac.compare_digest", spy)
    assert bearer_matches(f"Bearer {TOKEN}", TOKEN)
    assert calls == [(TOKEN.encode(), TOKEN.encode())]


def test_with_a_token_every_request_needs_it() -> None:
    g = guard(token=TOKEN)
    assert g.evaluate(headers(authorization=f"Bearer {TOKEN}")) is None
    assert g.evaluate(headers()) == 401
    assert g.evaluate(headers(authorization="Bearer wrong")) == 401
    assert g.evaluate(headers(authorization=[f"Bearer {TOKEN}", f"Bearer {TOKEN}"])) == 401


def test_the_token_can_be_asked_for_on_some_paths_only() -> None:
    g = guard(token=TOKEN, token_paths=("/api",))
    ok = headers(authorization=f"Bearer {TOKEN}")
    for path in ("/api", "/api/tools", "/api/versions/a/b/report"):
        assert g.evaluate(headers(), path) == 401 and g.evaluate(ok, path) is None, path
    for path in ("/", "/static/app.js", "/apiary", "/api-docs", "/x/api"):
        assert g.evaluate(headers(), path) is None, path
    # Host and Origin are checked on every path, with or without the token.
    assert g.evaluate(headers("evil.com"), "/") == 403
    assert g.evaluate(headers(origin="https://evil.example"), "/static/app.js") == 403


def test_an_unknown_path_and_an_empty_path_list_fail_closed() -> None:
    assert guard(token=TOKEN, token_paths=("/api",)).evaluate(headers(), None) == 401
    for paths in ((), ("/",), ("", "//")):
        g = guard(token=TOKEN, token_paths=paths)
        assert g.evaluate(headers(), "/anything") == 401, paths
    assert guard(token=TOKEN).evaluate(headers(), "/anything") == 401  # no token_paths: every path, as before


def test_the_guard_passes_the_request_path_to_evaluate() -> None:
    g = guard(token=TOKEN, token_paths=("/api",))
    sent, reached = run_asgi(g, {"type": "http", "path": "/", "headers": headers()})
    assert reached and sent[0]["status"] == 200
    sent, reached = run_asgi(g, {"type": "http", "path": "/api/tools", "headers": headers()})
    assert not reached and sent[0]["status"] == 401
    sent, reached = run_asgi(g, {"type": "http", "headers": headers()})  # a scope with no path
    assert not reached and sent[0]["status"] == 401


def test_the_log_line_names_the_allow_list_settings_of_its_own_listener(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="toolforge.netguard")
    guard(env_var="TOOLFORGE_DASHBOARD_TOKEN", what="the dashboard").evaluate(headers("evil.com"), "/")
    guard().evaluate(headers("evil.com"), "/")
    text = caplog.text
    assert "TOOLFORGE_DASHBOARD_ALLOWED_*" in text and "TOOLFORGE_SERVER_ALLOWED_*" in text


def test_the_refusal_can_name_another_variable() -> None:
    with pytest.raises(ConfigError) as caught:
        guard(bind_host="0.0.0.0", env_var="TOOLFORGE_DASHBOARD_TOKEN", what="the dashboard")
    assert "TOOLFORGE_DASHBOARD_TOKEN" in str(caught.value) and "the dashboard" in str(caught.value)


def test_without_a_token_a_loopback_listener_needs_no_header() -> None:
    assert guard().evaluate(headers()) is None


# --- Host and Origin --------------------------------------------------------------------------------------------------------------


def test_host_must_be_the_listener_or_a_loopback_name_with_its_port() -> None:
    g = guard()
    for ok in ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "LOCALHOST:8765"]:
        assert g.evaluate(headers(ok)) is None, ok
    for bad in [
        "evil.com",
        "evil.com:8765",
        "127.0.0.1",
        "127.0.0.1:1",
        "localhost.evil.com:8765",
        "127.0.0.1:8765@evil.com",
    ]:
        assert g.evaluate(headers(bad)) == 403, bad
    assert g.evaluate(headers(None)) == 403
    assert g.evaluate(headers(host=None) + [(b"host", b"a"), (b"host", b"b")]) == 403


def test_two_host_headers_are_refused_even_when_one_is_good() -> None:
    assert guard().evaluate([(b"host", b"127.0.0.1:8765"), (b"host", b"evil.com")]) == 403


def test_a_specific_bind_address_and_configured_hosts() -> None:
    g = guard(bind_host="10.0.0.5", token=TOKEN, allowed_hosts=["tools.internal", "proxy.example.com:443"])
    auth = {"authorization": f"Bearer {TOKEN}"}
    assert g.evaluate(headers("10.0.0.5:8765", **auth)) is None
    assert (
        g.evaluate(headers("tools.internal", **auth)) is None
    )  # a configured name is accepted as written (a proxy on 80/443)
    assert g.evaluate(headers("tools.internal:8765", **auth)) is None
    assert g.evaluate(headers("proxy.example.com:443", **auth)) is None
    assert g.evaluate(headers("127.0.0.1:8765", **auth)) == 403
    assert "10.0.0.5:8765" in host_header_values("10.0.0.5", 8765)


def test_origin_rules() -> None:
    assert origin_allowed(None)
    for ok in ["http://localhost:3000", "http://127.0.0.1", "https://localhost", "http://[::1]:9"]:
        assert origin_allowed(ok), ok
    for bad in [
        "null",
        "",
        "http://evil.com",
        "http://localhost.evil.com",
        "file://",
        "http://localhost/path",
        "http://user@localhost",
        "ftp://localhost",
        "localhost",
    ]:
        assert not origin_allowed(bad), bad
    assert origin_allowed("https://app.example.com", ["https://app.example.com/"])
    assert not origin_allowed("https://app.example.com:8443", ["https://app.example.com"])


def test_a_bad_origin_is_403_and_checked_before_the_token() -> None:
    g = guard(token=TOKEN)
    auth = {"authorization": f"Bearer {TOKEN}"}
    assert g.evaluate(headers(origin="http://evil.com", **auth)) == 403
    assert g.evaluate(headers(origin="http://localhost:5173", **auth)) is None
    assert (
        g.evaluate(headers(origin="http://evil.com")) == 403
    )  # no token and a bad origin: 403, the more specific refusal
    assert g.evaluate(headers(origin=["http://localhost", "http://localhost"], **auth)) == 403


# --- As an ASGI application ---------------------------------------------------------------------------------------------------------


def run_asgi(g: NetGuard, scope: dict) -> tuple[list[dict], bool]:
    sent: list[dict] = []
    reached = False

    async def inner(scope, receive, send):
        nonlocal reached
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    g._app = inner

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    anyio.run(g, scope, receive, send)
    return sent, reached


def test_a_refused_request_never_reaches_the_application() -> None:
    g = guard(token=TOKEN)
    sent, reached = run_asgi(g, {"type": "http", "headers": headers()})
    assert not reached and sent[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"] and sent[1]["body"] == b""
    sent, reached = run_asgi(
        g, {"type": "http", "headers": headers("evil.com", authorization=f"Bearer {TOKEN}")}
    )
    assert not reached and sent[0]["status"] == 403 and sent[1]["body"] == b""


def test_an_allowed_request_reaches_the_application() -> None:
    sent, reached = run_asgi(guard(), {"type": "http", "headers": headers()})
    assert reached and sent[0]["status"] == 200


def test_lifespan_passes_through_and_websockets_are_closed() -> None:
    g = guard(token=TOKEN)
    _, reached = run_asgi(g, {"type": "lifespan"})
    assert reached
    sent, reached = run_asgi(g, {"type": "websocket", "headers": headers(authorization=f"Bearer {TOKEN}")})
    assert not reached and sent == [{"type": "websocket.close", "code": 1008}]


# --- The token never leaks -----------------------------------------------------------------------------------------------------------


def test_the_token_is_not_in_repr_or_in_any_log_line(caplog: pytest.LogCaptureFixture) -> None:
    g = guard(token=TOKEN)
    assert TOKEN not in repr(g) and TOKEN not in str(vars(g).get("_hosts"))
    with caplog.at_level(logging.DEBUG):
        g.evaluate(headers())
        g.evaluate(headers(authorization="Bearer wrong"))
        g.evaluate(headers(host=f"{TOKEN}.evil.com"))  # a Host header that contains the token is redacted
        g.evaluate(headers(origin=f"http://{TOKEN}.evil.com", authorization=f"Bearer {TOKEN}"))
    assert caplog.records
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert all(TOKEN not in str(record.args) for record in caplog.records)
