"""Fetch and validate JSON from HTTP API."""

from __future__ import annotations

import urllib.request
import urllib.error
import json
from typing import Any

try:  # inside the sandbox the SDK provides ToolError (P05); the fallback lets the tests run anywhere
    from toolforge_sdk import ToolError
except ImportError:

    class ToolError(Exception):
        """Stand-in for toolforge_sdk.ToolError: an expected, handled failure."""


def run(args: dict) -> dict:
    url = args["url"]
    method = args.get("method", "GET")
    headers = args.get("headers", {})

    req = urllib.request.Request(url, method=method, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw_data = response.read()
            data = json.loads(raw_data.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise ToolError(f"Network error: {e.reason}")
    except json.JSONDecodeError as e:
        raise ToolError(f"Invalid JSON response: {e}")

    return {"status": "ok", "data": data}