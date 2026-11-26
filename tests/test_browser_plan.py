"""Browser plans (P14b): the closed vocabulary and its validator, templates, the executor against a scripted browser, and the
recorded-page browser. The manifest rules for a plan are covered code by code in `tests/test_package_rules.py`.

No browser, no Docker, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from toolforge.adapters import browser_fixture, browser_plan
from toolforge.adapters.browser_plan import PlanError, PlanFailure
from toolforge.models.package import ToolPackage

HOST = "news.example.org"
HOSTS = [HOST]
ARGS = {"topic", "password", "api_key"}
GOTO = {"step": "goto", "url": f"https://{HOST}/front"}
READ = {"step": "extract_text", "field": "body"}


def plan(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"steps": list(steps)}


def problems(value: object, hosts: list[str] = HOSTS) -> list[str]:
    found = browser_plan.validate_plan(value, hosts=hosts, arg_names=ARGS)
    return [f"{issue.path}: {issue.message}" for issue in found]


def refused(value: object, needle: str, hosts: list[str] = HOSTS) -> None:
    found = problems(value, hosts)
    assert any(needle in item for item in found), (needle, found)


# --- The vocabulary ----------------------------------------------------------------------------------------------------------


def test_a_plan_with_every_step_is_valid() -> None:
    everything = plan(
        GOTO,
        {"step": "type", "selector": "input[name=q]", "text": "{{topic}}", "submit": True},
        {"step": "wait_for", "ms": 100},
        {"step": "click", "selector": "a.next"},
        {"step": "screenshot"},
        {"step": "extract_text", "field": "body", "after": "{{topic}}", "before": "End", "max_chars": 100},
        {"step": "extract_text", "field": "rows", "as": "lines", "contains": "a", "skip": 1, "limit": 5},
        {"step": "extract_attr", "field": "links", "attr": "href", "match": "{{topic}}", "limit": 3},
    )
    assert problems(everything) == []
    assert sorted(browser_plan.STEPS) == sorted(
        ["goto", "click", "type", "extract_text", "extract_attr", "wait_for", "screenshot"]
    )


@pytest.mark.parametrize(
    "name", ["evaluate", "script", "back", "download", "upload", "press", "goto ", "GOTO", ""]
)
def test_a_step_outside_the_vocabulary_is_refused(name: str) -> None:
    refused(plan(GOTO, {"step": name}, READ), "unknown step")


@pytest.mark.parametrize(
    ("step", "needle"),
    [
        ({"step": "goto", "url": f"https://{HOST}/", "headers": {"a": "b"}}, "no argument 'headers'"),
        ({"step": "click", "selector": "a", "ref": 3}, "no argument 'ref'"),
        ({"step": "screenshot", "path": "/tmp/x"}, "no argument 'path'"),
        ({"step": "click"}, "click needs selector"),
        ({"step": "type", "selector": "input"}, "type needs text"),
        ({"step": "wait_for", "ms": 0}, "must be 1 to 5000"),
        ({"step": "wait_for", "ms": 5001}, "must be 1 to 5000"),
        ({"step": "wait_for", "ms": True}, "whole number"),
        ({"step": "wait_for", "ms": 1.5}, "whole number"),
        ({"step": "type", "selector": "input", "text": "a", "submit": "yes"}, "true or false"),
        ({"step": "click", "selector": 7}, "must be text"),
        ({"step": "click", "selector": ""}, "must be 1 to 300"),
        ({"step": "click", "selector": "a" * 301}, "must be 1 to 300"),
        ({"step": "extract_text", "field": "Bad Name"}, "lower-case name"),
        ({"step": "extract_text", "field": "x", "as": "html"}, "one of: text, lines"),
        ({"step": "extract_attr", "field": "x", "attr": "innerHTML"}, "one of: href"),
        ({"step": "extract_attr", "field": "x"}, "needs attr"),
        ({"step": "extract_text", "field": "x", "limit": 51}, "must be 1 to 50"),
    ],
)
def test_a_wrong_argument_is_refused(step: dict[str, Any], needle: str) -> None:
    refused(plan(GOTO, step, READ), needle)


@pytest.mark.parametrize(
    "value",
    [None, [], "steps", {}, {"steps": {}}, {"steps": [GOTO, READ], "name": "x"}, {"steps": []}],
)
def test_something_that_is_not_a_plan_is_refused(value: object) -> None:
    assert problems(value)


def test_the_limits_are_enforced() -> None:
    refused(plan(GOTO, *[{"step": "wait_for", "ms": 1}] * 30), "1 to 30 steps")
    refused(plan(*[GOTO] * 6, READ), "at most 5 goto")
    refused(plan(GOTO, *[{"step": "click", "selector": "a"}] * 11, READ), "at most 10 click")
    refused(plan(GOTO, *[{"step": "type", "selector": "i", "text": "a"}] * 6, READ), "at most 5 type")
    refused(plan(GOTO, *[{"step": "screenshot"}] * 3, READ), "at most 2 screenshot")
    refused(plan(GOTO, *[{"step": "wait_for", "ms": 4000}] * 4, READ), "waits add up to 16000")
    refused(plan(GOTO, READ, READ), "named twice")
    refused(plan(GOTO), "at least one extract")
    refused(plan(READ), "put a goto step before it")


def test_a_plan_file_that_is_too_large_or_not_json_is_refused() -> None:
    for text in ("{" * 100000, "not json", "[" * 5000 + "]" * 5000):
        value, issues = browser_plan.parse_plan(text)
        assert value is None or isinstance(value, list), text[:10]
        assert issues or isinstance(value, list)
    value, issues = browser_plan.parse_plan(json.dumps({"steps": ["x" * 20000]}))
    assert value is None and "larger than 16 KB" in issues[0].message


# --- Hosts -------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,x",
        "https://other.example.org/",
        f"https://a.{HOST}/",
        f"https://{HOST}.evil.example/",
        f"https://{HOST}@evil.example/",
        f"https://user:pw@{HOST}/",
        f"https://{HOST}:8443/",
        "https://NEWS.example.org/",
        "https://127.0.0.1/",
        "https://localhost/",
        "https:///front",
        f"https://{HOST}\\@evil.example/",
        f"https://{HOST}/a b",
        f"https://{HOST}/a‮b",
        f"https://{HOST}/a/../b",
        f"https://{HOST}/a/%2e%2e/b",
        f"https://{HOST}/" + "a" * 600,
    ],
)
def test_a_goto_outside_the_approved_https_hosts_is_refused(url: str) -> None:
    refused(plan({"step": "goto", "url": url}, READ), "steps[0].url")


@pytest.mark.parametrize(
    "url",
    [
        "https://{{topic}}/",
        "https://{{topic}}.example.org/",
        f"https://{HOST}{{{{topic}}}}/x",
        "{{topic}}",
        f"https://{HOST}/{{{{nope}}}}",
        f"https://{HOST}/{{{{topic}}",
        f"https://{HOST}/{{{{ topic.x }}}}",
        f"https://{HOST}/topic}}}}",
    ],
)
def test_a_template_never_reaches_the_host_and_must_name_an_input(url: str) -> None:
    refused(plan({"step": "goto", "url": url}, READ), "steps[0].url")


def test_a_numeric_host_is_refused_even_when_declared() -> None:
    refused(plan({"step": "goto", "url": "https://10.0.0.1/"}, READ), "no IP address", hosts=["10.0.0.1"])


# --- Selectors and credentials ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["click", "type"])
@pytest.mark.parametrize("selector", ["{{topic}}", "a[href='{{topic}}']", "#x}}"])
def test_an_argument_never_goes_into_a_selector(step: str, selector: str) -> None:
    body = {"step": step, "selector": selector, **({"text": "a"} if step == "type" else {})}
    refused(plan(GOTO, body, READ), "never goes into a selector")


@pytest.mark.parametrize(
    "selector",
    [
        "input[type=password]",
        "input[type='PASSWORD']",
        "#passwd",
        "#user_passcode",
        "input[name=pwd]",
        "input[name=api_key]",
        "input[name=apikey]",
        "#client-secret",
        "[name=access_token]",
        "input[autocomplete=one-time-code]",
        "#otp",
        "input[name=credential]",
    ],
)
@pytest.mark.parametrize("step", ["click", "type"])
def test_a_credential_selector_is_refused(step: str, selector: str) -> None:
    body = {"step": step, "selector": selector, **({"text": "a"} if step == "type" else {})}
    refused(plan(GOTO, body, READ), "credential field")


@pytest.mark.parametrize("selector", ["#hotpot", "input[name=footprint]", "a.compass", "input[name=q]"])
def test_an_ordinary_selector_is_not_mistaken_for_a_credential(selector: str) -> None:
    assert problems(plan(GOTO, {"step": "click", "selector": selector}, READ)) == []


@pytest.mark.parametrize("name", ["password", "api_key"])
def test_an_argument_named_like_a_credential_cannot_be_put_into_a_page(name: str) -> None:
    template = f"{{{{{name}}}}}"
    refused(
        plan(GOTO, {"step": "type", "selector": "input", "text": template}, READ),
        "never carries a credential",
    )


@pytest.mark.parametrize(
    "step",
    [
        {"step": "click", "selector": "javascript:alert(1)"},
        {"step": "click", "selector": "a​b"},
        {"step": "type", "selector": "input", "text": "a\nb"},
        {"step": "type", "selector": "input", "text": "a‮b"},
    ],
)
def test_script_and_invisible_characters_are_refused(step: dict[str, Any]) -> None:
    assert problems(plan(GOTO, step, READ))


# --- Rendering ---------------------------------------------------------------------------------------------------------------

SEARCH = plan(
    {"step": "goto", "url": f"https://{HOST}/topic/{{{{topic}}}}?q={{{{topic}}}}"},
    {"step": "type", "selector": "input[name=q]", "text": "{{ topic }}"},
    {"step": "extract_text", "field": "body", "after": "{{topic}}"},
)


@pytest.mark.parametrize(
    "value",
    [
        "@evil.example",
        "//evil.example/x",
        "https://evil.example/x",
        "a/b?c=d#e",
        "%2f%2fevil.example",
        "x y",
        '"], input[type=password], a["',
        "{{topic}}",
    ],
)
def test_a_value_is_encoded_into_the_url_and_copied_as_text_elsewhere(value: str) -> None:
    steps = browser_plan.render_plan(SEARCH, {"topic": value}, HOSTS)
    url = steps[0]["url"]
    assert browser_plan.host_of(url) == HOST
    path, query = url[len(f"https://{HOST}") :].split("?", 1)
    assert (
        path.count("/") == 2
        and "#" not in url
        and "@" not in url
        and " " not in url
        and query.startswith("q=")
    )
    assert steps[1] == {"step": "type", "selector": "input[name=q]", "text": value}
    assert steps[2]["after"] == value
    assert SEARCH["steps"][1]["text"] == "{{ topic }}"  # rendering never changes the plan


def test_a_number_can_fill_a_template() -> None:
    assert browser_plan.render_plan(SEARCH, {"topic": 7}, HOSTS)[1]["text"] == "7"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({}, "missing_arg"),
        ({"topic": None}, "bad_arg"),
        ({"topic": True}, "bad_arg"),
        ({"topic": ["a"]}, "bad_arg"),
        ({"topic": {"a": 1}}, "bad_arg"),
        ({"topic": "a\u0000"}, "bad_arg"),
        ({"topic": "a\nb"}, "bad_arg"),
        ({"topic": "‮gnp.exe"}, "bad_arg"),
        ({"topic": "\ud800"}, "bad_arg"),
        ({"topic": "x" * 301}, "too_long"),
        ({"topic": 10**5000}, "too_long"),
        ({"topic": "é" * 300}, "too_long"),  # six characters each once percent-encoded
        ({"topic": "."}, "bad_path"),
        ({"topic": ".."}, "bad_path"),
    ],
)
def test_a_value_that_cannot_be_placed_safely_is_refused(args: dict[str, Any], code: str) -> None:
    with pytest.raises(PlanError) as error:
        browser_plan.render_plan(SEARCH, args, HOSTS)
    assert error.value.code == code


def test_the_host_is_checked_again_after_rendering() -> None:
    with pytest.raises(PlanError) as error:
        browser_plan.render_plan(SEARCH, {"topic": "a"}, ["other.example.org"])
    assert error.value.code == "host_not_approved"


@pytest.mark.parametrize(
    ("url", "host"),
    [
        (f"https://{HOST}/x", HOST),
        (f"https://{HOST.upper()}/x", HOST),
        (f"http://{HOST}/x", None),
        (f"https://a@{HOST}/", None),
        (f"https://{HOST}:443/", None),
        (f"https://{HOST}:x/", None),
        ("https://[::1", None),
        ("about:blank", None),
        (None, None),
        (7, None),
    ],
)
def test_host_of(url: object, host: str | None) -> None:
    assert browser_plan.host_of(url) == host


def test_what_a_plan_says_about_itself() -> None:
    assert browser_plan.template_args(SEARCH) == ["topic"]
    assert browser_plan.output_fields(SEARCH) == ["body"]
    assert browser_plan.goto_hosts(SEARCH) == [HOST]


# --- The executor ------------------------------------------------------------------------------------------------------------

PAGE_URL = f"https://{HOST}/front?secret=1"


class Scripted:
    """A browser that answers every call with the next reply, or the same page forever."""

    def __init__(self, *replies: Any, **page: Any) -> None:
        self.replies = list(replies)
        self.page = {"ok": True, "url": PAGE_URL, "text": "", "elements": [], **page}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, action: str, params: dict[str, Any], timeout_s: int = 40) -> Any:
        self.calls.append((action, params))
        return self.replies.pop(0) if self.replies else dict(self.page)


def run(browser: Any, *steps: dict[str, Any]) -> browser_plan.StepsResult:
    return browser_plan.run_steps(list(steps), browser, hosts=HOSTS)


def test_each_step_becomes_one_worker_action_and_no_screenshot_is_asked_for_by_the_way() -> None:
    browser = Scripted()
    result = run(
        browser,
        GOTO,
        {"step": "type", "selector": "input", "text": "hi", "submit": True},
        {"step": "click", "selector": "a"},
        {"step": "wait_for", "ms": 250},
        {"step": "screenshot"},
        READ,
    )
    assert browser.calls == [
        ("goto", {"url": GOTO["url"], "shot": False}),
        ("type", {"selector": "input", "text": "hi", "submit": True, "shot": False}),
        ("click", {"selector": "a", "shot": False}),
        ("extract", {"wait_ms": 250, "shot": False}),
        ("screenshot", {"shot": True}),
    ]
    assert (result.steps_run, result.screenshots) == (6, 1)
    assert result.visited == [f"{HOST}/front"]  # never the query


def test_a_screenshot_the_worker_could_not_take_is_not_counted() -> None:
    ok = {"ok": True, "url": PAGE_URL, "text": ""}
    browser = Scripted(ok, {**ok, "screenshot_error": "TimeoutError"})
    assert run(browser, GOTO, {"step": "screenshot"}, READ).screenshots == 0


def test_an_extract_asks_for_the_page_when_the_last_reply_had_no_text() -> None:
    browser = Scripted({"ok": True, "url": PAGE_URL}, {"ok": True, "url": PAGE_URL, "text": "now"})
    assert run(browser, GOTO, READ).fields == {"body": "now"}
    assert [action for action, _ in browser.calls] == ["goto", "extract"]


TEXT = "Menu\nTop stories\n  Alpha one \n\nBeta two\nalpha three\nMore\nfooter"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, TEXT),
        ({"after": "Top stories", "before": "More"}, "Alpha one \n\nBeta two\nalpha three"),
        ({"after": "Menu", "max_chars": 1000, "before": "Beta"}, "Top stories\n  Alpha one"),
        ({"as": "lines", "after": "stories", "before": "More"}, ["Alpha one", "Beta two", "alpha three"]),
        ({"as": "lines", "contains": "ALPHA"}, ["Alpha one", "alpha three"]),
        ({"as": "lines", "after": "stories", "skip": 1, "limit": 1}, ["Beta two"]),
        ({"as": "lines", "after": "footer"}, []),
    ],
)
def test_extract_text(extra: dict[str, Any], expected: Any) -> None:
    assert run(Scripted(text=TEXT), GOTO, {**READ, **extra}).fields == {"body": expected}


def test_a_long_value_is_cut() -> None:
    value = run(Scripted(text="a" * 9000), GOTO, {**READ, "max_chars": 50}).fields["body"]
    assert len(value) <= 50


@pytest.mark.parametrize("anchor", ["after", "before"])
def test_a_missing_anchor_fails_the_run(anchor: str) -> None:
    with pytest.raises(PlanFailure) as failure:
        run(Scripted(text="nothing here"), GOTO, {**READ, anchor: "Top stories"})
    assert failure.value.kind == "page" and "was not found" in failure.value.message


ELEMENTS = [
    {"tag": "a", "type": "", "label": "Story one", "href": f"https://{HOST}/1", "name": ""},
    {"tag": "input", "type": "password", "label": "hunter2 story", "href": "", "name": "story_pw"},
    {"tag": "a", "type": "", "label": "About", "href": f"https://{HOST}/about", "name": ""},
    {"tag": "a", "type": "", "label": "Story two", "href": f"https://{HOST}/2", "name": ""},
    "not an element",
]


def test_extract_attr_matches_labels_and_skips_password_fields() -> None:
    links = {"step": "extract_attr", "field": "links", "attr": "href", "match": "STORY"}
    labels = {"step": "extract_attr", "field": "labels", "attr": "label"}
    names = {"step": "extract_attr", "field": "names", "attr": "name"}
    first = {**labels, "field": "first", "limit": 1}
    fields = run(Scripted(elements=ELEMENTS), GOTO, links, labels, names, first).fields
    assert fields["links"] == [f"https://{HOST}/1", f"https://{HOST}/2"]
    assert fields["labels"] == ["Story one", "About", "Story two"]
    assert fields["names"] == [] and fields["first"] == ["Story one"]


def test_page_text_is_cleaned_of_invisible_characters() -> None:
    browser = Scripted(text="Hi‮​ there", elements=[{"label": "a​b"}])
    fields = run(browser, GOTO, READ, {"step": "extract_attr", "field": "labels", "attr": "label"}).fields
    assert fields == {"body": "Hi there", "labels": ["ab"]}


@pytest.mark.parametrize("reply", [{"ok": False, "error": "TimeoutError: no element"}, {}, None, "ok", []])
def test_a_browser_that_says_no_fails_the_run(reply: Any) -> None:
    with pytest.raises(PlanFailure) as failure:
        run(Scripted(reply), GOTO, READ)
    assert failure.value.kind == "browser" and "step 1 (goto)" in failure.value.message


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        f"https://sub.{HOST}/",
        f"http://{HOST}/",
        f"https://{HOST}:444/",
        "chrome-error://chromewebdata/",
        "data:text/html,x",
    ],
)
@pytest.mark.parametrize("action", ["goto", "click", "wait_for"])
def test_a_page_that_leaves_the_approved_hosts_stops_the_run(action: str, url: str) -> None:
    ok = {"ok": True, "url": PAGE_URL, "text": ""}
    gone = {"ok": True, "url": url, "text": "secret of the other host"}
    step = {
        "goto": GOTO,
        "click": {"step": "click", "selector": "a"},
        "wait_for": {"step": "wait_for", "ms": 1},
    }
    replies = [gone] if action == "goto" else [ok, gone]
    steps = [GOTO, READ] if action == "goto" else [GOTO, step[action], READ]
    with pytest.raises(PlanFailure) as failure:
        run(Scripted(*replies), *steps)
    assert failure.value.kind == "host"


def test_a_page_not_loaded_yet_is_not_a_departure() -> None:
    browser = Scripted({"ok": True, "url": "about:blank", "text": "x"})
    assert run(browser, GOTO, READ).fields == {"body": "x"}


# --- Recorded pages ----------------------------------------------------------------------------------------------------------


def fixtures(*pages: dict[str, Any]) -> dict[str, str]:
    return {"fixtures/a.json": json.dumps({"pages": list(pages)})}


FIRST = {
    "url": f"https://{HOST}/",
    "text": "home",
    "clicks": {"a.next": f"https://{HOST}/next"},
    "inputs": ["input[name=q]"],
    "on_submit": f"https://{HOST}/next",
}
SECOND = {"url": f"https://{HOST}/next", "text": "next page"}


def test_the_recorded_browser_answers_like_the_worker() -> None:
    browser, why = browser_fixture.load_fixtures(fixtures(FIRST, SECOND))
    assert browser is not None and why == []
    assert browser.call("click", {"selector": "a.next"})["ok"] is False  # no page yet
    assert browser.call("goto", {"url": f"https://{HOST}/missing"})["ok"] is False
    assert browser.call("goto", {"url": FIRST["url"]})["text"] == "home"
    assert browser.call("click", {"selector": "a.other"})["ok"] is False
    assert browser.call("type", {"selector": "input[name=other]", "text": "x"})["ok"] is False
    assert browser.call("type", {"selector": "input[name=q]", "text": "x"})["text"] == "home"
    assert (
        browser.call("type", {"selector": "input[name=q]", "text": "y", "submit": True})["text"]
        == "next page"
    )
    assert browser.typed == [("input[name=q]", "x"), ("input[name=q]", "y")]
    assert "text" not in browser.call("screenshot", {})
    assert browser.call("back", {})["ok"] is False
    browser.call("goto", {"url": FIRST["url"]})
    assert browser.call("click", {"selector": "a.next"})["url"] == SECOND["url"]


def test_no_fixture_files_is_not_a_problem_by_itself() -> None:
    assert browser_fixture.load_fixtures({"plan.json": "{}", "fixtures/readme.md": "x"}) == (None, [])


@pytest.mark.parametrize(
    "files",
    [
        {"fixtures/a.json": "nope"},
        {"fixtures/a.json": json.dumps({"pages": {}})},
        {"fixtures/a.json": json.dumps(["x"])},
        fixtures({"url": f"http://{HOST}/"}),
        fixtures({"text": "no url"}),
        fixtures({"url": FIRST["url"], "script": "x"}),
        fixtures({"url": FIRST["url"], "text": 5}),
        fixtures({"url": FIRST["url"], "text": "x" * 12001}),
        fixtures({"url": FIRST["url"], "elements": [{"onclick": "x"}]}),
        fixtures({"url": FIRST["url"], "elements": [{"label": 1}]}),
        fixtures({"url": FIRST["url"], "elements": [{}] * 41}),
        fixtures({"url": FIRST["url"], "clicks": ["a"]}),
        fixtures({"url": FIRST["url"], "inputs": "a"}),
        fixtures({"url": FIRST["url"], "on_submit": 1}),
        fixtures(SECOND, SECOND),
        fixtures(*[{"url": f"https://{HOST}/{n}"} for n in range(13)]),
    ],
)
def test_a_wrong_recording_is_reported_and_gives_no_browser(files: dict[str, str]) -> None:
    browser, why = browser_fixture.load_fixtures(files)
    assert browser is None and why


def test_the_reference_pack_runs_against_its_own_pages() -> None:
    pack = ToolPackage.from_dir(
        Path(__file__).resolve().parent.parent / "examples" / "toolpacks" / "browser_headlines"
    )
    value, _ = browser_plan.parse_plan(pack.files["plan.json"])
    hosts = pack.manifest.permissions.network
    for example in pack.manifest.examples:
        browser, why = browser_fixture.load_fixtures(pack.files)
        assert browser is not None, why
        steps = browser_plan.render_plan(value, example.input, hosts)
        assert {"fields": browser_plan.run_steps(steps, browser, hosts=hosts).fields} == example.output
