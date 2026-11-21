"""The checks every ToolForge network listener makes before it answers anything: the MCP server over HTTP (P07) and the dashboard (P17).

Nothing here knows about MCP. It is one pure ASGI wrapper (`NetGuard`) and the small functions it is made of, so both listeners use
exactly the same code.

Security properties, each proved in `tests/test_netguard.py` (and, through a real server app, in `tests/test_server_http.py`):

* A listener bound to anything but a loopback address must have a bearer token, or it refuses to start (`require_token`). An empty or
  blank token counts as no token, so a `TOOLFORGE_SERVER_TOKEN=` line left in `.env` cannot switch the check off.
* With a token, every request must carry `Authorization: Bearer <token>`, compared in constant time. Anything else is a bare 401.
* The `Host` header must be one the listener expects (its own bound address, a loopback name, or an operator-configured value) and an
  `Origin` header, when present, must be a loopback origin or a configured one. This is what stops a web page from reaching a
  loopback listener through DNS rebinding. Anything else is a bare 403. A request with two `Host` or two `Origin` headers is refused.
* The token is never logged, never put in a message and never shown by `repr`. A request header that contains it is redacted if it
  is ever logged.

What this does not do: it does not encrypt (use a TLS-terminating proxy for anything but a trusted network), it does not rate-limit,
and "loopback" means the address, not the user: any local process can connect to a loopback listener.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from toolforge.errors import ConfigError

log = logging.getLogger(__name__)

LOOPBACK_HOST_HEADERS = ("127.0.0.1", "localhost", "[::1]")  # the spellings a local client puts in `Host`
WILDCARD_ADDRESSES = ("", "0.0.0.0", "::")  # "listen on every interface"
_LOG_CHARS = 100

Headers = Sequence[tuple[bytes, bytes]]


def _bare(host: str) -> str:
    """The host without brackets, a trailing dot, or case: `[::1]` -> `::1`, `LocalHost.` -> `localhost`."""
    text = str(host).strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text.rstrip(".") if not _is_ip(text) else text


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def is_loopback(host: str) -> bool:
    """True for `localhost` and for a loopback IP address (127.0.0.0/8, ::1, ::ffff:127.0.0.1).

    Any other name is not loopback, even if it resolves to 127.0.0.1 today: a name can be pointed anywhere, which is exactly what a
    DNS rebinding attack does.
    """
    text = _bare(host)
    if text == "localhost":
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)


def token_text(token: object) -> str | None:
    """The token as text, or None when there is none (unset, empty or blank). Accepts a `SecretStr` or a plain string."""
    value = token.get_secret_value() if hasattr(token, "get_secret_value") else token
    if not isinstance(value, str):
        return None
    return value.strip() or None


def require_token(
    host: str, token: object, *, env_var: str = "TOOLFORGE_SERVER_TOKEN", what: str = "the server"
) -> None:
    """Refuse to listen beyond loopback without a token. Raises `ConfigError` whose message names the variable, never a value."""
    if is_loopback(host) or token_text(token) is not None:
        return
    shown = host if host.strip() else "(all interfaces)"
    raise ConfigError(
        f"{what} would listen on {shown}, which is not a loopback address, and no token is set",
        hint=f"set {env_var} to a long random value (clients send it as `Authorization: Bearer <token>`), or bind to 127.0.0.1",
    )


def bearer_matches(authorization: str | None, token: str) -> bool:
    """Is `authorization` exactly `Bearer <token>`? The comparison takes the same time wherever the first difference is."""
    if not authorization or not token:
        return False
    scheme, _, presented = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip().encode("utf-8"), token.encode("utf-8"))


def host_header_values(bind_host: str, port: int, extra: Iterable[str] = ()) -> frozenset[str]:
    """Every `Host` header value a legitimate client of a listener on `bind_host:port` can send.

    A loopback or all-interfaces listener is reached by the loopback names. A listener on one specific address is reached by that
    address. `extra` is what the operator configured: a value is accepted as written and, when it has no port, with this port.
    """
    bound = _bare(bind_host)
    names: set[str] = set()
    if is_loopback(bound) or bound in WILDCARD_ADDRESSES:
        names.update(LOOPBACK_HOST_HEADERS)
    if bound not in WILDCARD_ADDRESSES and not is_loopback(bound):
        names.add(f"[{bound}]" if ":" in bound else bound)
    values = {f"{name}:{port}" for name in names}
    for raw in extra:
        entry = str(raw).strip().lower()
        if not entry:
            continue
        values.add(entry)
        if not _has_port(entry):
            values.add(f"{entry}:{port}")
    return frozenset(values)


def _has_port(host_value: str) -> bool:
    if host_value.startswith("["):
        return host_value.rfind("]:") != -1
    return host_value.count(":") == 1


def origin_allowed(origin: str | None, extra: Iterable[str] = ()) -> bool:
    """May a request with this `Origin` header proceed? No header at all is fine (a command line client sends none).

    A present header must be a loopback origin (`http://localhost:3000` and the like, any port) or one the operator configured. The
    value `null` (a sandboxed frame or a file) and anything that is not a plain `scheme://host[:port]` is refused.
    """
    if origin is None:
        return True
    text = origin.strip().lower()
    if text in {str(item).strip().lower().rstrip("/") for item in extra}:
        return True
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return False
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
    ):
        return False
    return is_loopback(host)


def _values(headers: Headers, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in headers if key.lower() == name]


class NetGuard:
    """Wrap an ASGI app so that only requests that pass `evaluate` reach it. Lifespan events pass straight through."""

    def __init__(
        self,
        app: Any,
        *,
        bind_host: str,
        port: int,
        token: object = None,
        allowed_hosts: Iterable[str] = (),
        allowed_origins: Iterable[str] = (),
    ) -> None:
        self._app = app
        self._token = token_text(token)
        self._hosts = host_header_values(bind_host, port, allowed_hosts)
        self._origins = tuple(
            str(origin).strip().lower() for origin in allowed_origins if str(origin).strip()
        )
        require_token(bind_host, self._token)

    def __repr__(self) -> str:
        return f"NetGuard(hosts={sorted(self._hosts)!r}, token={'set' if self._token else 'none'})"

    def evaluate(self, headers: Headers) -> int | None:
        """None when the request may proceed, else the status code to answer with: 403 for `Host` or `Origin`, 401 for the token."""
        hosts, origins = _values(headers, b"host"), _values(headers, b"origin")
        if len(hosts) != 1 or hosts[0].strip().lower() not in self._hosts:
            self._refuse("Host", hosts)
            return 403
        if len(origins) > 1 or (origins and not origin_allowed(origins[0], self._origins)):
            self._refuse("Origin", origins)
            return 403
        if self._token is not None:
            authorization = _values(headers, b"authorization")
            if len(authorization) != 1 or not bearer_matches(authorization[0], self._token):
                log.warning("refused a request: missing or wrong bearer token")
                return 401
        return None

    def _refuse(self, which: str, values: list[str]) -> None:
        shown = [self._shown(value) for value in values[:2]]
        log.warning(
            "refused a request: the %s header %s is not allowed (see TOOLFORGE_SERVER_ALLOWED_*)",
            which,
            shown,
        )

    def _shown(self, value: str) -> str:
        if self._token and self._token in value:
            return "[redacted]"
        return repr(value[:_LOG_CHARS])

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self._app(scope, receive, send)
            return
        if kind != "http":  # no WebSocket or other protocol is part of MCP over HTTP
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        status = self.evaluate(scope.get("headers") or [])
        if status is None:
            await self._app(scope, receive, send)
            return
        headers = [(b"content-length", b"0")]
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


__all__ = [
    "LOOPBACK_HOST_HEADERS",
    "NetGuard",
    "bearer_matches",
    "host_header_values",
    "is_loopback",
    "origin_allowed",
    "require_token",
    "token_text",
]
