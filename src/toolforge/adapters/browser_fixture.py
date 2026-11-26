"""A browser that replays recorded pages, for gate G3's dry run and for tests. It is not a browser and runs no page script.

A package of kind `browser_plan` carries `fixtures/*.json`: snapshots of the pages its examples visit, in the shape MiniLocker's browser
worker answers with (`url`, `title`, `text`, `elements`). The dry run (G3) runs the plan against these and compares the extracted
fields with each example's `output`. That proves the plan is well formed and extracts what its author says; it cannot prove the live
site still looks like the recording, which only the live smoke run after approval shows.

```json
{"pages": [{"url": "https://news.example/front", "title": "Front page", "text": "Top stories\\nOne\\nTwo",
            "elements": [{"tag": "a", "type": "", "label": "One", "href": "https://news.example/one", "name": ""}],
            "clicks": {"a.next": "https://news.example/page2"}, "inputs": ["input[name=q]"]}]}
```

`clicks` maps a selector to the URL it leads to; `inputs` lists selectors that accept typed text (a `submit` goes to the page named
by `on_submit`, a URL, when there is one). Anything not recorded fails the way the real browser would: with `ok: false`.

Proven by `tests/test_browser_plan.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_PAGES = 12
MAX_TEXT = 12000  # the browser worker's own cap on `text`
MAX_ELEMENTS = 40
FIXTURE_PREFIX = "fixtures/"
_ELEMENT_KEYS = {"tag", "type", "label", "href", "name"}
_PAGE_KEYS = {"url", "title", "text", "elements", "clicks", "inputs", "on_submit"}


class FixtureBrowser:
    """Answers `call(action, params)` from recorded pages. Starts on no page, like the real worker."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = {page["url"]: page for page in pages}
        self.current: dict[str, Any] | None = None
        self.typed: list[tuple[str, str]] = []

    def _snapshot(self, with_text: bool = True) -> dict[str, Any]:
        page = self.current or {}
        out: dict[str, Any] = {
            "ok": True,
            "url": page.get("url", ""),
            "title": page.get("title", ""),
            "status": None,
        }
        if with_text:
            out["text"] = page.get("text", "")
            out["elements"] = list(page.get("elements", []))
        return out

    def _go(self, url: object) -> dict[str, Any]:
        page = self.pages.get(url) if isinstance(url, str) else None
        if page is None:
            return {"ok": False, "error": "no recorded page for that address"}
        self.current = page
        return {**self._snapshot(), "status": 200}

    def call(self, action: str, params: dict[str, Any], timeout_s: int = 0) -> dict[str, Any]:
        if action == "ping":
            return {"ok": True}
        if action == "goto":
            return self._go(params.get("url"))
        if self.current is None:
            return {"ok": False, "error": "there is no page yet"}
        selector = params.get("selector")
        if action == "click":
            target = self.current.get("clicks", {}).get(selector)
            return self._go(target) if target else {"ok": False, "error": "no element matches that selector"}
        if action == "type":
            if selector not in self.current.get("inputs", []):
                return {"ok": False, "error": "no input matches that selector"}
            self.typed.append((selector, str(params.get("text", ""))))
            if params.get("submit") and self.current.get("on_submit"):
                return self._go(self.current["on_submit"])
            return self._snapshot()
        if action == "extract":
            return self._snapshot()
        if action == "screenshot":
            return {**self._snapshot(with_text=False), "screenshot_b64": ""}
        return {"ok": False, "error": f"unknown action {action[:20]!r}"}


def _page_problem(page: object, where: str) -> str | None:
    if not isinstance(page, dict) or set(page) - _PAGE_KEYS:
        return f"{where} is not a page object with only {', '.join(sorted(_PAGE_KEYS))}"
    if not isinstance(page.get("url"), str) or not page["url"].startswith("https://"):
        return f"{where} needs a url that starts with https://"
    if not isinstance(page.get("text", ""), str) or len(page.get("text", "")) > MAX_TEXT:
        return f"{where}: text must be text of at most {MAX_TEXT} characters"
    if not isinstance(page.get("title", ""), str):
        return f"{where}: title must be text"
    elements = page.get("elements", [])
    if not isinstance(elements, list) or len(elements) > MAX_ELEMENTS:
        return f"{where}: elements is a list of at most {MAX_ELEMENTS}"
    for element in elements:
        if (
            not isinstance(element, dict)
            or set(element) - _ELEMENT_KEYS
            or any(not isinstance(v, str) for v in element.values())
        ):
            return f"{where}: each element has only {', '.join(sorted(_ELEMENT_KEYS))}, all text"
    clicks = page.get("clicks", {})
    if not isinstance(clicks, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in clicks.items()
    ):
        return f"{where}: clicks maps a selector to a URL"
    inputs = page.get("inputs", [])
    if not isinstance(inputs, list) or not all(isinstance(s, str) for s in inputs):
        return f"{where}: inputs is a list of selectors"
    if not isinstance(page.get("on_submit", ""), str):
        return f"{where}: on_submit is a URL"
    return None


def load_fixtures(files: Mapping[str, str]) -> tuple[FixtureBrowser | None, list[str]]:
    """`(browser, problems)` from the package's `fixtures/*.json`. No fixture files gives `(None, [])`."""
    pages: list[dict[str, Any]] = []
    problems: list[str] = []
    for path in sorted(files):
        if not (path.startswith(FIXTURE_PREFIX) and path.endswith(".json")):
            continue
        try:
            data = json.loads(files[path])
        except (ValueError, RecursionError):
            problems.append(f"{path} is not valid JSON")
            continue
        listed = data.get("pages") if isinstance(data, dict) and "pages" in data else [data]
        if not isinstance(listed, list):
            problems.append(f"{path}: pages must be a list")
            continue
        for number, page in enumerate(listed):
            problem = _page_problem(page, f"{path} page {number + 1}")
            if problem:
                problems.append(problem)
            else:
                pages.append(page)
    addresses = [page["url"] for page in pages]
    for url in sorted({u for u in addresses if addresses.count(u) > 1}):
        problems.append(f"the page {url[:80]!r} is recorded more than once")
    if len(pages) > MAX_PAGES:
        problems.append(f"the fixtures hold {len(pages)} pages; the most allowed is {MAX_PAGES}")
    return (FixtureBrowser(pages) if pages and not problems else None), problems


__all__ = ["FixtureBrowser", "load_fixtures"]
