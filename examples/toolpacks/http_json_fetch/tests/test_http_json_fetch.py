import json
import urllib.error

import tool
from tool import ToolError, run


def serving(status, payload):
    """Make `_fetch` answer from a recorded response and remember the URLs asked for."""
    asked = []
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

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


def test_returns_the_whole_document_and_asks_the_declared_host_only():
    asked = serving(200, {"full_name": "python/cpython", "stargazers_count": 7})
    result = run({"path": "/repos/python/cpython"})
    assert result == {"status": 200, "data": {"full_name": "python/cpython", "stargazers_count": 7}}
    assert asked == ["https://api.github.com/repos/python/cpython"]


def test_pick_keeps_only_the_named_top_level_keys():
    serving(200, {"a": 1, "b": 2, "c": 3})
    assert run({"path": "/x", "pick": ["c", "a", "missing"]})["data"] == {"c": 3, "a": 1}


def test_query_values_are_encoded_and_sorted():
    asked = serving(200, [])
    run({"path": "/search", "query": {"q": "a b&c=d", "page": "2"}})
    assert asked == ["https://api.github.com/search?page=2&q=a+b%26c%3Dd"]


def test_other_status_codes_and_non_json_are_handled_errors():
    serving(404, {"message": "Not Found"})
    refused({"path": "/nope"}, "HTTP status 404")
    serving(200, b"<html>not json</html>")
    refused({"path": "/x"}, "not JSON")
    serving(200, b"x" * 70_000)
    refused({"path": "/x"}, "larger than")


def test_pick_on_a_list_is_refused():
    serving(200, [1, 2])
    refused({"path": "/x", "pick": ["a"]}, "not one")


def test_a_path_cannot_change_the_host_or_climb():
    serving(200, {})
    for path in (
        "//evil.example/x",
        "@evil.example",
        "x",
        "/a/../b",
        "/a/%2e%2e/b",
        "/a b",
        "/a?x=1",
        "/a#frag",
    ):
        refused({"path": path}, "path must start with /")


def test_a_network_failure_is_a_handled_error():
    def down(url, timeout):
        raise urllib.error.URLError("no route")

    tool._fetch = down
    refused({"path": "/x"}, "could not reach")


def test_a_redirect_is_not_followed():
    handler = tool._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
