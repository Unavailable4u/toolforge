"""Browser plans (P14b): a forged web tool is **data, not code**.

A tool of `kind: "browser_plan"` is one JSON file, `plan.json`: a list of steps from a closed vocabulary (`goto`, `click`, `type`,
`wait_for`, `screenshot`, `extract_text`, `extract_attr`). The host validates it, checks every `goto` host against the hosts the
operator approved, runs it in MiniLocker's browser sandbox, and returns only the fields the `extract_*` steps name. Nothing in a plan
runs as code: there is no step that evaluates script.

A `{{arg}}` template can put the caller's text in four places only: the path, query or fragment of a `goto` URL (percent-encoded,
never the host), the text a `type` step types, the `after`, `before` and `contains` anchors of `extract_text`, and the `match` of
`extract_attr`. It can never go into a **selector**, so a caller cannot choose which element is clicked or typed into.

This module is plain Python with no browser and no Docker: the vocabulary and its limits (`STEPS`, `LIMITS`), `validate_plan`,
`render_plan` (templates), and `run_steps`, which drives anything with a `call(action, params) -> dict` method (MiniLocker's
`BrowserSandbox`, or the recorded-page `FixtureBrowser` that gate G3 uses). Page text is untrusted: every extracted value goes through
`textsafety.sanitize`, and the server fences a tool's output on top of that.

What the real browser worker offers decides the vocabulary. It cannot read an element by selector, only the page's visible text and a
list of up to 40 interactive elements (tag, type, label, href, name), so `extract_text` works on the visible text (between literal
anchors, or line by line) and `extract_attr` on that element list. This is recorded in `docs/guide/DEVIATIONS.md`.

The credential check is a heuristic on names (a selector or an argument called like a password or a token is refused). It cannot
know what an element is. What holds regardless: a plan declares no secrets, gets no brokered service and has no way to read one.

Proven by `tests/test_browser_plan.py` (every rule, template injection attempts, the executor against a scripted browser).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit

PLAN_KIND = "browser_plan"
PLAN_FILE = "plan.json"
MAX_PLAN_BYTES = 16 * 1024
MAX_STEPS = 30
MAX_TEXT = 300  # any string in a step
MAX_URL = 500
MAX_ITEMS = 50  # lines or attributes one extract step may return
MAX_FIELD_CHARS = 2000  # one extracted value
MAX_WAIT_MS = 5000  # the browser worker caps a wait at this too
MAX_TOTAL_WAIT_MS = 15000
MAX_FIELDS = 20
LIMITS: dict[str, int] = {"goto": 5, "click": 10, "type": 5, "wait_for": 5, "screenshot": 2}

NAME = re.compile(r"[a-z][a-z0-9_]{0,30}")
TEMPLATE = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,30})\s*\}\}")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
_NUMERIC_HOST = re.compile(r"[0-9.]+")
_CREDENTIAL = re.compile(
    r"passw|passcode|(?<![a-z])pwd(?![a-z])|secret|token|(?<![a-z])otp(?![a-z])|one-time-code|credential|api[-_]?key",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Spec:
    """One argument of a step. `kind` is `url` or `text` (both may hold `{{arg}}`), or `selector`, `name`, `bool`, `int` or `enum`
    (never templated)."""

    kind: str
    required: bool = False
    choices: tuple[str, ...] = ()
    low: int = 0
    high: int = 0


STEPS: dict[str, dict[str, Spec]] = {
    "goto": {"url": Spec("url", True)},
    "click": {"selector": Spec("selector", True)},
    "type": {"selector": Spec("selector", True), "text": Spec("text", True), "submit": Spec("bool")},
    "wait_for": {"ms": Spec("int", True, low=1, high=MAX_WAIT_MS)},
    "screenshot": {},
    "extract_text": {
        "field": Spec("name", True),
        "as": Spec("enum", choices=("text", "lines")),
        "after": Spec("text"),
        "before": Spec("text"),
        "contains": Spec("text"),
        "skip": Spec("int", low=0, high=1000),
        "limit": Spec("int", low=1, high=MAX_ITEMS),
        "max_chars": Spec("int", low=1, high=MAX_FIELD_CHARS),
    },
    "extract_attr": {
        "field": Spec("name", True),
        "attr": Spec("enum", True, choices=("href", "label", "name", "type")),
        "match": Spec("text"),
        "limit": Spec("int", low=1, high=MAX_ITEMS),
    },
}
EXTRACTING = ("extract_text", "extract_attr")


@dataclass(frozen=True)
class PlanIssue:
    path: str  # `steps[2].url`
    message: str


class PlanError(ValueError):
    """A plan cannot be run with these arguments. `code` is stable: `missing_arg`, `bad_arg`, `bad_path`, `host_not_approved`,
    `too_long`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PlanFailure(Exception):
    """A step failed while running. `kind` is `browser` (the browser said no), `host` (the page left the approved hosts), `page`
    (the page did not have what the plan expected) or `timeout` (the run's time ran out)."""

    def __init__(self, kind: str, message: str, step: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.step = step


# --- Reading and validating ------------------------------------------------------------------------------------------------


def parse_plan(text: str) -> tuple[Any, list[PlanIssue]]:
    """`(plan, issues)` from the text of `plan.json`. A file that is not JSON, or is too large, gives `(None, issues)`."""
    if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
        return None, [PlanIssue("plan.json", f"the plan is larger than {MAX_PLAN_BYTES // 1024} KB")]
    try:
        return json.loads(text), []
    except (ValueError, RecursionError):
        return None, [PlanIssue("plan.json", "the plan is not valid JSON")]


def _has_hidden(text: str) -> bool:
    """A control, invisible, surrogate or unassigned character (Unicode category C)."""
    return any(unicodedata.category(char)[0] == "C" for char in text)


def _dot_segment(url: str) -> bool:
    """A `.` or `..` path segment, written plainly or percent-encoded: a browser would walk up the path with it."""
    path = re.split(r"[?#]", url[len("https://") :], maxsplit=1)[0]
    return any(unquote(segment) in (".", "..") for segment in path.split("/")[1:])


def _host_problem(host: str) -> str | None:
    if not HOST.fullmatch(host) or _NUMERIC_HOST.fullmatch(host):
        return "must be a bare lower-case host name with at least one dot (no IP address, port or wildcard)"
    return None


def _template_problem(text: str, arg_names: Collection[str]) -> str | None:
    """Why `{{...}}` in `text` is wrong, or None. Only `{{name}}` of a declared input property is allowed."""
    for found in TEMPLATE.finditer(text):
        if found.group(1) not in arg_names:
            return f"{{{{{found.group(1)}}}}} is not a property of input_schema"
    rest = TEMPLATE.sub("", text)
    if "{{" in rest or "}}" in rest:
        return "has a stray {{ or }}; a template is {{name}} with a lower-case name"
    return None


def _check_url(value: str, hosts: Collection[str], arg_names: Collection[str]) -> str | None:
    if not value.startswith("https://"):
        return "must start with https:// (plain http is not allowed)"
    if len(value) > MAX_URL:
        return f"is longer than {MAX_URL} characters"
    if _has_hidden(value) or any(char.isspace() for char in value) or "\\" in value:
        return "contains a space, a backslash or a control or invisible character"
    rest = value[len("https://") :]
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "{" in authority or "}" in authority:
        return "must not use a template in the host: the host is fixed in the plan"
    problem = _host_problem(authority)
    if problem:
        return f"has the host {authority[:60]!r}, which {problem}"
    if authority not in hosts:
        return f"goes to {authority}, which is not in permissions.network"
    if _dot_segment(value):
        return "has a . or .. path segment; write the real path"
    return _template_problem(value[len("https://") + len(authority) :], arg_names)


def _check_value(spec: Spec, value: Any, arg_names: Collection[str], hosts: Collection[str]) -> str | None:
    kind = spec.kind
    if kind == "bool":
        return None if isinstance(value, bool) else "must be true or false"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be a whole number"
        return None if spec.low <= value <= spec.high else f"must be {spec.low} to {spec.high}"
    if not isinstance(value, str):
        return "must be text"
    if kind == "url":
        return _check_url(value, hosts, arg_names)
    if kind == "name":
        return (
            None
            if NAME.fullmatch(value)
            else "must be a lower-case name like headlines (letters, digits, underscores)"
        )
    if kind == "enum":
        return None if value in spec.choices else f"must be one of: {', '.join(spec.choices)}"
    if not value or len(value) > MAX_TEXT:
        return f"must be 1 to {MAX_TEXT} characters"
    if _has_hidden(value):
        return "contains a control or invisible character"
    if kind == "selector":
        if "{{" in value or "}}" in value:
            return "must be fixed in the plan: an argument never goes into a selector"
        if _CREDENTIAL.search(value):
            return "names a credential field; a plan never touches one"
        if value.lstrip().lower().startswith("javascript"):
            return "must be an element selector, not script"
        return None
    problem = _template_problem(value, arg_names)
    if problem:
        return problem
    for name in TEMPLATE.findall(value):
        if _CREDENTIAL.search(name):
            return f"puts the argument {name!r} into the page; a plan never carries a credential"
    return None


def validate_plan(plan: object, *, hosts: Sequence[str], arg_names: Collection[str]) -> list[PlanIssue]:
    """Every problem with `plan`, at once. `hosts` are the manifest's `permissions.network`; `arg_names` the input properties."""
    issues: list[PlanIssue] = []
    if not isinstance(plan, dict) or set(plan) != {"steps"} or not isinstance(plan["steps"], list):
        return [PlanIssue("plan", 'the plan is an object with one key, "steps", holding a list of steps')]
    steps = plan["steps"]
    if not 1 <= len(steps) <= MAX_STEPS:
        return [PlanIssue("steps", f"a plan has 1 to {MAX_STEPS} steps, not {len(steps)}")]
    counts: dict[str, int] = {}
    fields: set[str] = set()
    visited_page = False
    waited = 0
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        if not isinstance(step, dict) or not isinstance(step.get("step"), str):
            issues.append(PlanIssue(where, 'every step is an object with a "step" name'))
            continue
        name = step["step"]
        if name not in STEPS:
            issues.append(PlanIssue(where, f"unknown step {name[:30]!r}; the steps are: {', '.join(STEPS)}"))
            continue
        specs = STEPS[name]
        for key in step:
            if key != "step" and key not in specs:
                issues.append(PlanIssue(f"{where}.{key[:30]}", f"{name} has no argument {key[:30]!r}"))
        for key, spec in specs.items():
            if key not in step:
                if spec.required:
                    issues.append(PlanIssue(f"{where}.{key}", f"{name} needs {key}"))
                continue
            problem = _check_value(spec, step[key], arg_names, hosts)
            if problem:
                issues.append(PlanIssue(f"{where}.{key}", f"{key} {problem}"))
        counts[name] = counts.get(name, 0) + 1
        if name == "goto":
            visited_page = True
        elif not visited_page:
            issues.append(PlanIssue(where, f"{name} needs a page: put a goto step before it"))
        if name == "wait_for" and isinstance(step.get("ms"), int) and not isinstance(step.get("ms"), bool):
            waited += step["ms"]
        if name in EXTRACTING and isinstance(step.get("field"), str):
            if step["field"] in fields:
                issues.append(PlanIssue(f"{where}.field", f"the field {step['field']!r} is named twice"))
            fields.add(step["field"])
    for name, limit in LIMITS.items():
        if counts.get(name, 0) > limit:
            issues.append(PlanIssue("steps", f"a plan has at most {limit} {name} steps, not {counts[name]}"))
    if waited > MAX_TOTAL_WAIT_MS:
        issues.append(
            PlanIssue("steps", f"the waits add up to {waited} ms; the most allowed is {MAX_TOTAL_WAIT_MS}")
        )
    if not fields:
        issues.append(
            PlanIssue(
                "steps", "a plan needs at least one extract_text or extract_attr step: that is its output"
            )
        )
    if len(fields) > MAX_FIELDS:
        issues.append(PlanIssue("steps", f"a plan has at most {MAX_FIELDS} output fields"))
    return issues


def template_args(plan: Mapping[str, Any]) -> list[str]:
    """The argument names the plan's templates use (sorted, no repeats)."""
    names: set[str] = set()
    for step in plan.get("steps", []):
        for key, value in step.items():
            if isinstance(value, str) and _templated(step, key):
                names.update(TEMPLATE.findall(value))
    return sorted(names)


def _templated(step: Mapping[str, Any], key: str) -> bool:
    spec = STEPS.get(str(step.get("step")), {}).get(key)
    return spec is not None and spec.kind in ("url", "text")


def output_fields(plan: Mapping[str, Any]) -> list[str]:
    return [s["field"] for s in plan.get("steps", []) if s.get("step") in EXTRACTING and "field" in s]


def goto_hosts(plan: Mapping[str, Any]) -> list[str]:
    """The hosts a plan opens, from its literal `goto` URLs (sorted, no repeats)."""
    found = set()
    for step in plan.get("steps", []):
        if step.get("step") == "goto" and isinstance(step.get("url"), str):
            found.add(re.split(r"[/?#]", step["url"][len("https://") :], maxsplit=1)[0])
    return sorted(found)


# --- Rendering -------------------------------------------------------------------------------------------------------------


def _argument(args: Mapping[str, Any], name: str) -> str:
    if name not in args:
        raise PlanError("missing_arg", f"the plan needs the argument {name!r}")
    value = args[name]
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise PlanError("bad_arg", f"the argument {name!r} must be text or a number to go into a plan")
    try:
        text = str(value)
    except ValueError:  # an integer too long to write out
        raise PlanError("too_long", f"the argument {name!r} is too long") from None
    if len(text) > MAX_TEXT:
        raise PlanError("too_long", f"the argument {name!r} is longer than {MAX_TEXT} characters")
    if _has_hidden(text):
        raise PlanError("bad_arg", f"the argument {name!r} contains a control or invisible character")
    return text


def _fill(text: str, args: Mapping[str, Any], *, encode: bool) -> str:
    def put(found: re.Match[str]) -> str:
        value = _argument(args, found.group(1))
        return quote(value, safe="") if encode else value

    return TEMPLATE.sub(put, text)


def host_of(url: object) -> str | None:
    """The host of an https URL with no user name, password or port; None for anything else."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port is not None or not parts.hostname:
        return None
    return parts.hostname.lower()


def render_plan(
    plan: Mapping[str, Any], args: Mapping[str, Any], hosts: Collection[str]
) -> list[dict[str, Any]]:
    """The steps with every `{{arg}}` filled in from `args`. Raises `PlanError`.

    A value in a URL is percent-encoded, so it cannot add a `/`, `?`, `@` or `#` of its own; after filling, the host of every `goto` is
    read again and must still be one of `hosts`, whatever was substituted, and a value that would make a `.` or `..` path segment is
    refused. A rendered string longer than the limit is refused. Selectors are copied as they are.
    """
    rendered: list[dict[str, Any]] = []
    for step in plan["steps"]:
        out: dict[str, Any] = {}
        for key, value in step.items():
            if isinstance(value, str) and _templated(step, key):
                value = _fill(value, args, encode=step["step"] == "goto" and key == "url")
                if len(value) > (MAX_URL if key == "url" else MAX_TEXT):
                    raise PlanError(
                        "too_long", f"the argument makes the {key} of a {step['step']} step too long"
                    )
            out[key] = value
        if out["step"] == "goto":
            if host_of(out["url"]) not in hosts:
                raise PlanError("host_not_approved", "the address in a goto step is not on an approved host")
            if _dot_segment(out["url"]):
                raise PlanError("bad_path", "an argument would make a . or .. path segment in an address")
        rendered.append(out)
    return rendered


# --- Running ---------------------------------------------------------------------------------------------------------------


class Browser(Protocol):
    def call(self, action: str, params: dict[str, Any], timeout_s: int = ...) -> dict[str, Any]: ...


@dataclass
class StepsResult:
    fields: dict[str, Any] = field(default_factory=dict)
    visited: list[str] = field(default_factory=list)  # host and path of each page opened, never the query
    steps_run: int = 0
    screenshots: int = 0


def _clean(value: str, limit: int) -> str:
    # Imported here: the package model imports this module, and must stay light.
    from toolforge import textsafety

    return textsafety.sanitize(value, max_chars=limit)


def _text_value(text: str, step: Mapping[str, Any], index: int) -> Any:
    body = text
    for key in ("after", "before"):
        anchor = step.get(key)
        if anchor is None:
            continue
        at = body.find(anchor)
        if at < 0:
            raise PlanFailure(
                "page", f"the text {anchor[:40]!r} for {step['field']} was not found on the page", index
            )
        body = body[at + len(anchor) :] if key == "after" else body[:at]
    if step.get("as", "text") == "lines":
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if step.get("contains") is not None:
            lines = [line for line in lines if step["contains"].lower() in line.lower()]
        lines = lines[step.get("skip", 0) :][: step.get("limit", MAX_ITEMS)]
        return [_clean(line, MAX_FIELD_CHARS) for line in lines]
    return _clean(body.strip(), step.get("max_chars", MAX_FIELD_CHARS))


def _attr_value(elements: object, step: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for element in elements if isinstance(elements, list) else []:
        # The worker uses an input's current value as its label, so a password field is skipped whole.
        if not isinstance(element, dict) or str(element.get("type", "")).lower() == "password":
            continue
        if (
            step.get("match") is not None
            and step["match"].lower() not in str(element.get("label", "")).lower()
        ):
            continue
        value = str(element.get(step["attr"], "")).strip()
        if value:
            found.append(_clean(value, MAX_FIELD_CHARS))
        if len(found) >= step.get("limit", MAX_ITEMS):
            break
    return found


def _page_url_ok(reply: Mapping[str, Any], hosts: Collection[str], index: int) -> None:
    url = reply.get("url")
    if url in (None, "", "about:blank"):
        return
    if host_of(url) not in hosts:
        raise PlanFailure("host", "the page moved to a host that is not approved, so the plan stopped", index)


def run_steps(steps: Sequence[Mapping[str, Any]], browser: Browser, *, hosts: Collection[str]) -> StepsResult:
    """Run rendered steps against `browser`. Raises `PlanFailure`. Returns the extracted fields and what was visited.

    After every step the page's address is read back: a redirect or a click that leaves the approved hosts stops the plan, whatever
    the browser's own proxy would have allowed.
    """
    result = StepsResult()
    last: dict[str, Any] | None = None
    for index, step in enumerate(steps):
        name = step["step"]
        if name in EXTRACTING:
            if last is None or "text" not in last:
                last = _ask(browser, "extract", {"shot": False}, index, hosts)
            if name == "extract_text":
                result.fields[step["field"]] = _text_value(str(last.get("text", "")), step, index)
            else:
                result.fields[step["field"]] = _attr_value(last.get("elements"), step)
        elif name == "goto":
            last = _ask(browser, "goto", {"url": step["url"], "shot": False}, index, hosts)
            parts = urlsplit(str(last.get("url") or step["url"]))
            result.visited.append(f"{parts.hostname or ''}{parts.path}"[:200])
        elif name == "click":
            last = _ask(browser, "click", {"selector": step["selector"], "shot": False}, index, hosts)
        elif name == "type":
            params = {
                "selector": step["selector"],
                "text": step["text"],
                "submit": bool(step.get("submit")),
                "shot": False,
            }
            last = _ask(browser, "type", params, index, hosts)
        elif name == "wait_for":
            last = _ask(browser, "extract", {"wait_ms": step["ms"], "shot": False}, index, hosts)
        elif name == "screenshot":
            reply = _ask(browser, "screenshot", {"shot": True}, index, hosts)
            if "screenshot_error" not in reply:  # the image is dropped: only that one was taken is kept
                result.screenshots += 1
        result.steps_run += 1
    return result


def _ask(
    browser: Browser, action: str, params: dict[str, Any], index: int, hosts: Collection[str]
) -> dict[str, Any]:
    reply = browser.call(action, params)
    if not isinstance(reply, dict) or not reply.get("ok"):
        why = reply.get("error") if isinstance(reply, dict) else None
        raise PlanFailure(
            "browser", f"step {index + 1} ({action}) failed: {_clean(str(why or 'no reply'), 200)}", index
        )
    _page_url_ok(reply, hosts, index)
    return reply


__all__ = [
    "EXTRACTING",
    "LIMITS",
    "PLAN_FILE",
    "PLAN_KIND",
    "STEPS",
    "PlanError",
    "PlanFailure",
    "PlanIssue",
    "StepsResult",
    "goto_hosts",
    "host_of",
    "output_fields",
    "parse_plan",
    "render_plan",
    "run_steps",
    "template_args",
    "validate_plan",
]
