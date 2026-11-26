# Recipe: a tool that calls a web API

This is documentation written by the operator of this ToolForge, not output from a tool. Read `package-format` first. A tool that
reaches a host is the most sensitive kind: it needs a human operator's approval, and the operator reads exactly what you ask for.

## What you declare

- `permissions.network`: the bare host names the tool talks to (`api.github.com`, no scheme, no path, no port, no wildcard), at most 8.
  Every other host is blocked by the egress proxy, whatever the code does.
- `permissions.secrets`: **service names**, never values. A tool never holds a secret. The operator configures a service once; inside
  the sandbox the tool finds `MINILOCKER_BROKER_URL` and calls `$MINILOCKER_BROKER_URL/<service>/<path>`, and the proxy adds the real
  credential to the request. If the API needs no credential, leave `secrets` empty: ask for the least that works.

## Rules

1. **The host is fixed in the code.** Build the URL as `https://<host constant><path>`. The caller chooses the path, the query and
   the values, never the host or the scheme. Check the path with a regular expression in the code (start with `/`, letters, digits and
   `._~/%-`, no `//`, no `..` even percent-encoded), and encode query values with `urllib.parse.urlencode`.
2. **Do not follow redirects**: a redirect can lead to a host nobody approved. Use a `HTTPRedirectHandler` whose `redirect_request`
   returns `None`, and report the 3xx status.
3. **Bound the response.** Read at most a fixed number of bytes (`response.read(LIMIT + 1)`), refuse a longer body, set a timeout (10 s),
   and parse JSON yourself with `json.loads` so that "not JSON" is a `ToolError`.
4. **Everything that can go wrong is a `ToolError`**: a network failure (`URLError`, `OSError`), a non-2xx status (name the status), a
   body that is not JSON.
5. **Send nothing from the environment** and no cookies. A `User-Agent` and `Accept` header are enough.
6. **Make the HTTP layer one function** (`_fetch(url, timeout) -> (status, body)`) so that the tests replace it with recorded
   responses. No gate has a network: your tests must pass with none.

## Examples and the live run

G3, G4 and G6 run with **no network**, so an example that needs the real service must be marked `"live": true`. A live example is skipped
by those gates and runs once, after the operator approves and before the version becomes active; only success is needed (its answer is
not compared). Give it an `output` anyway, or the manifest warns that no example checks behaviour. Keep `pattern` off the input
schema: G4 builds its inputs from the schema, and a schema made only of patterns leaves it with nothing to try, so the gate is skipped.
The gates cannot show that the live site still looks as you recorded it. Only the live run does.

## A complete small package: one JSON document from a fixed host

```json manifest
{
  "name": "zen_fetch",
  "version": "1.0.0",
  "kind": "stateless",
  "description": "GET one JSON document from api.github.com over HTTPS (the path is chosen by the caller) and return its status and body.",
  "when_to_use": "The task needs a public GitHub REST API document that needs no token.",
  "entrypoint": "tool.py:run",
  "input_schema": {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["path"],
    "additionalProperties": false
  },
  "permissions": {"network": ["api.github.com"]},
  "examples": [
    {"input": {"path": "/repos/python/cpython"}, "output": {"status": 200, "data": {}}, "live": true}
  ]
}
```

```python tool.py
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from toolforge_sdk import ToolError

HOST = "api.github.com"  # the same host as permissions.network
LIMIT = 60000
PATH = re.compile(r"/[A-Za-z0-9._~/%-]{0,200}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "toolforge-tool"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read(LIMIT + 1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(LIMIT + 1)


def run(args):
    path = args["path"]
    if not PATH.fullmatch(path) or "//" in path or ".." in urllib.parse.unquote(path).split("/"):
        raise ToolError("path must start with / and use only letters, digits and . _ ~ / % -")
    try:
        status, body = _fetch(f"https://{HOST}{path}", 10)
    except (urllib.error.URLError, OSError):
        raise ToolError(f"could not reach {HOST}") from None
    if len(body) > LIMIT:
        raise ToolError("the response is too large; ask for something smaller")
    if not 200 <= status < 300:
        raise ToolError(f"{HOST} answered with HTTP status {status}")
    try:
        return {"status": status, "data": json.loads(body.decode("utf-8"))}
    except (UnicodeDecodeError, ValueError):
        raise ToolError("the response is not JSON") from None
```

```python tests/test_zen_fetch.py
import json

import tool
from tool import ToolError, run


def answer(status, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    asked = []

    def fake(url, timeout):
        asked.append(url)
        return status, body

    tool._fetch = fake
    return asked


def refused(args, needle):
    try:
        run(args)
    except ToolError as error:
        assert needle in str(error), str(error)
    else:
        raise AssertionError("expected ToolError")


def test_returns_the_document_and_asks_only_the_fixed_host():
    asked = answer(200, {"name": "x"})
    assert run({"path": "/repos/a/b"}) == {"status": 200, "data": {"name": "x"}}
    assert asked == ["https://api.github.com/repos/a/b"]


def test_other_statuses_and_non_json_are_handled_errors():
    answer(404, {"message": "Not Found"})
    refused({"path": "/nope"}, "status 404")
    answer(200, b"<html>")
    refused({"path": "/x"}, "not JSON")


def test_a_path_cannot_change_the_host_or_climb():
    answer(200, {})
    for path in ("//evil.example/x", "@evil.example", "/a/../b", "/a/%2e%2e/b", "/a b"):
        refused({"path": path}, "path must start with /")
```

The full reference version (query values, a `pick` list, redirect test) is the pack `http_json_fetch` in `examples/toolpacks/`.
