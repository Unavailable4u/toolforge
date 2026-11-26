"""Fetch one JSON document with an HTTPS GET from a host declared in the manifest, and return it (or the keys asked for).

The HTTP layer is one small function, `_fetch`, so the tests give it recorded responses and no test needs a network. The host is fixed
in the manifest (`permissions.network`); the caller chooses only the path, the query and which top-level keys to keep. Redirects are
not followed (a redirect could lead to a host nobody approved), the body is read up to a limit, and nothing from the environment
is sent. This tool needs no token; a tool that does uses a brokered secret (see the http-api recipe).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

try:  # inside the sandbox the SDK provides ToolError; the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


HOST = "api.github.com"  # the same host as permissions.network in the manifest
MAX_BODY = 60_000
TIMEOUT_S = 10
_PATH = re.compile(r"/[A-Za-z0-9._~/%-]{0,200}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the 3xx answer is raised as an HTTPError and reported, never followed


def _fetch(url: str, timeout: int) -> tuple[int, bytes]:
    """`(status, body)` for a GET of `url`; the body is cut at MAX_BODY + 1 bytes so the caller can tell it was too long."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "toolforge-http-json-fetch"}
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_BODY + 1)


def build_url(path: str, query: dict[str, str]) -> str:
    if not _PATH.fullmatch(path) or "//" in path or ".." in urllib.parse.unquote(path).split("/"):
        raise ToolError("path must start with / and use only letters, digits and . _ ~ / % -")
    url = f"https://{HOST}{path}"
    return f"{url}?{urllib.parse.urlencode(sorted(query.items()))}" if query else url


def run(args: dict) -> dict:
    url = build_url(args["path"], args.get("query", {}))
    try:
        status, body = _fetch(url, TIMEOUT_S)
    except (urllib.error.URLError, OSError, ValueError):
        raise ToolError(
            f"could not reach {HOST}; check that the host is approved and the network is up"
        ) from None
    if len(body) > MAX_BODY:
        raise ToolError(
            f"the response is larger than {MAX_BODY // 1000} KB; ask for less with a more specific path or query"
        )
    if not 200 <= status < 300:
        raise ToolError(f"{HOST} answered with HTTP status {status}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ToolError("the response is not JSON") from None
    keys = args.get("pick", [])
    if keys:
        if not isinstance(data, dict):
            raise ToolError("pick works on a JSON object; this response is not one")
        data = {key: data[key] for key in keys if key in data}
    return {"status": status, "data": data}
