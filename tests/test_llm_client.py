"""The LLM client: parsing, the one nudge, retries and rate limits with a fake clock and transport, and what it refuses to send."""

from __future__ import annotations

import json
import random
import urllib.error
from typing import Any

import pytest

from toolforge.config import Settings
from toolforge.errors import LLMBadReply, LLMRefused, LLMUnavailable
from toolforge.llm import client as llm
from toolforge.llm.client import (
    FakeLLM,
    HttpResponse,
    LLMClient,
    RateLimiter,
    ReplyProblem,
    TransportError,
    client_from_settings,
    parse_json_object,
)
from toolforge.verify import secretscan

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class Clock:
    """Time that only moves when something sleeps, so no test waits."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Wire:
    """A transport that answers from a script and records every request."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout_s: float) -> HttpResponse:
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout_s})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def ok(text: str, *, usage: Any = None, finish: str = "stop", model: str | None = None) -> HttpResponse:
    document: dict[str, Any] = {"choices": [{"message": {"content": text}, "finish_reason": finish}]}
    if usage is not None:
        document["usage"] = usage
    if model:
        document["model"] = model
    return HttpResponse(200, {}, json.dumps(document).encode())


def status(code: int, headers: dict[str, str] | None = None, body: bytes = b"") -> HttpResponse:
    return HttpResponse(code, headers or {}, body)


def make(*answers: Any, clock: Clock | None = None, **options: Any) -> tuple[LLMClient, Wire, Clock]:
    clock = clock or Clock()
    wire = Wire(*answers)
    client = LLMClient(
        options.pop("base_url", "https://llm.test/v1/"),
        options.pop("model", "m-1"),
        options.pop("api_key", "sk-test-key-value"),
        transport=wire,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(1),
        wall_time=lambda: 5000.0,
        **options,
    )
    return client, wire, clock


# --- Reading a reply --------------------------------------------------------------------------------------------------------


def test_a_fence_prose_and_trailing_commas_are_tolerated() -> None:
    assert parse_json_object('```json\n{"a": 1,}\n```') == {"a": 1}
    assert parse_json_object('Sure, here it is: {"a": [1, 2,], "b": "x,}"} done') == {"a": [1, 2], "b": "x,}"}


@pytest.mark.parametrize(
    "text", ['{"a": [1, 2', '{"cases": [{"input": {"x": 1}', "no json", "", "   ", '{"a": NaN}']
)
def test_truncated_or_unreadable_json_is_never_completed(text: str) -> None:
    with pytest.raises(ReplyProblem):
        parse_json_object(text)


def test_a_cut_off_object_is_not_replaced_by_an_inner_fragment() -> None:
    with pytest.raises(ReplyProblem):
        parse_json_object('{"cases": [{"input": {"text": "x"}, "check": {"kind": "no_error"}}, {"input"')


def test_only_an_object_counts() -> None:
    with pytest.raises(ReplyProblem):
        parse_json_object("[1, 2, 3]")


# --- The request ------------------------------------------------------------------------------------------------------------


def test_a_request_sends_only_model_and_messages_and_records_the_cost() -> None:
    client, wire, _ = make(
        ok('{"a": 1}', usage={"prompt_tokens": 12, "completion_tokens": 3}, model="m-1-0613")
    )
    reply = client.complete_json("be careful", "the question", schema_hint="an object with a")
    call = wire.calls[0]
    assert call["url"] == "https://llm.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test-key-value"
    assert set(call["body"]) == {"model", "messages"}  # no vendor-specific parameter by default
    system = call["body"]["messages"][0]["content"]
    assert "be careful" in system and "an object with a" in system and "exactly one JSON object" in system
    assert reply.data == {"a": 1} and reply.model == "m-1-0613"
    assert (reply.prompt_tokens, reply.completion_tokens, reply.requests, reply.nudged) == (12, 3, 1, False)
    assert reply.latency_ms >= 0


def test_missing_token_counts_are_not_measured_not_zero() -> None:
    client, _, _ = make(ok('{"a": 1}'))
    reply = client.complete_json("s", "u")
    assert reply.prompt_tokens is None and reply.completion_tokens is None


def test_no_key_means_no_authorization_header() -> None:
    client, wire, _ = make(ok("{}"), api_key=None)
    client.complete_json("s", "u")
    assert "Authorization" not in wire.calls[0]["headers"]


def test_native_json_mode_adds_response_format_and_falls_back_when_the_server_rejects_it() -> None:
    client, wire, _ = make(status(400), ok('{"a": 1}'), json_mode="native")
    assert client.complete_json("s", "u").data == {"a": 1}
    assert wire.calls[0]["body"]["response_format"] == {"type": "json_object"}
    assert "response_format" not in wire.calls[1]["body"] and client.json_mode == "prompt"


def test_parts_style_content_is_joined() -> None:
    body = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": '{"a":'}, {"type": "text", "text": " 2}"}]}}
        ]
    }
    client, _, _ = make(HttpResponse(200, {}, json.dumps(body).encode()))
    assert client.complete_json("s", "u").data == {"a": 2}


def test_a_base_url_must_be_plain_http_or_https() -> None:
    for bad in ("ftp://x.test/", "file:///etc/passwd", "https://user:pw@x.test/", "no-scheme.test"):
        with pytest.raises(ValueError):
            LLMClient(bad, "m")
    with pytest.raises(ValueError):
        LLMClient("https://x.test/", "  ")


def test_the_key_is_never_in_the_repr() -> None:
    client, _, _ = make()
    assert "sk-test-key-value" not in repr(client) and "***" in repr(client)


# --- The nudge --------------------------------------------------------------------------------------------------------------


def test_one_parse_failure_earns_one_specific_nudge_and_the_costs_add_up() -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    client, wire, _ = make(ok("Sorry, I cannot do that", usage=usage), ok('{"a": 1}', usage=usage))
    reply = client.complete_json("s", "u")
    assert reply.data == {"a": 1} and reply.nudged and reply.requests == 2
    assert (reply.prompt_tokens, reply.completion_tokens) == (20, 10)
    last = wire.calls[1]["body"]["messages"][-1]["content"]
    assert "contained no JSON object" in last and "complete JSON object" in last
    assert wire.calls[1]["body"]["messages"][-2]["role"] == "assistant"


def test_a_cut_off_reply_is_nudged_not_repaired() -> None:
    client, wire, _ = make(ok('{"cases": [{"input": {"x": 1}', finish="length"), ok('{"cases": []}'))
    assert client.complete_json("s", "u").data == {"cases": []}
    assert "cut off" in wire.calls[1]["body"]["messages"][-1]["content"]


def test_a_reply_that_stopped_at_the_length_limit_is_not_used_even_if_it_parses() -> None:
    client, _, _ = make(ok('{"a": 1}', finish="length"), ok('{"a": 2}'))
    assert client.complete_json("s", "u").data == {"a": 2}


def test_a_shape_check_failure_counts_as_a_parse_failure() -> None:
    def need_cases(data: dict[str, Any]) -> None:
        if "cases" not in data:
            raise ReplyProblem("the JSON object has no `cases` list")

    client, wire, _ = make(ok('{"other": 1}'), ok('{"cases": []}'))
    assert client.complete_json("s", "u", check=need_cases).data == {"cases": []}
    assert "no `cases` list" in wire.calls[1]["body"]["messages"][-1]["content"]


def test_two_unusable_replies_in_a_row_raise_bad_reply() -> None:
    client, wire, _ = make(ok("nope"), ok("still nope"))
    with pytest.raises(LLMBadReply):
        client.complete_json("s", "u")
    assert len(wire.calls) == 2  # one question and one nudge, never more


def test_an_empty_or_malformed_envelope_is_an_unusable_reply() -> None:
    client, _, _ = make(HttpResponse(200, {}, b"<html>"), HttpResponse(200, {}, b'{"choices": []}'))
    with pytest.raises(LLMBadReply):
        client.complete_json("s", "u")


# --- Rate limits and outages ------------------------------------------------------------------------------------------------


def test_429_with_retry_after_waits_that_long_and_then_succeeds() -> None:
    client, wire, clock = make(status(429, {"retry-after": "7"}), ok('{"a": 1}'))
    assert client.complete_json("s", "u").data == {"a": 1}
    assert clock.slept == [7.0] and len(wire.calls) == 2


def test_retry_after_may_be_an_http_date() -> None:
    client, _, clock = make(status(429, {"retry-after": "Thu, 01 Jan 1970 01:23:30 GMT"}), ok("{}"))
    client.complete_json("s", "u")
    assert clock.slept == [
        pytest.approx(5010.0 - 5000.0)
    ]  # 01:23:30 is 5010 s after the epoch; wall_time says 5000


def test_repeated_429_ends_in_llm_unavailable_after_four_retries_with_growing_backoff() -> None:
    client, wire, clock = make(*[status(429) for _ in range(5)])
    with pytest.raises(LLMUnavailable) as caught:
        client.complete_json("s", "u")
    assert len(wire.calls) == 5 and len(clock.slept) == 4
    for n, delay in enumerate(clock.slept):
        assert 0.5 * 2**n <= delay <= 1.0 * 2**n  # exponential, with jitter
    assert "HTTP 429" in str(caught.value)


def test_a_wait_longer_than_the_budget_is_refused_without_sleeping() -> None:
    client, wire, clock = make(status(429, {"retry-after": "900"}), wall_budget_s=60)
    with pytest.raises(LLMUnavailable):
        client.complete_json("s", "u")
    assert clock.slept == [] and len(wire.calls) == 1


def test_5xx_and_connection_errors_are_retried() -> None:
    client, wire, _ = make(status(503), TransportError("TimeoutError"), ok('{"a": 1}'))
    assert client.complete_json("s", "u").data == {"a": 1} and len(wire.calls) == 3


def test_the_budget_is_shared_by_the_whole_question() -> None:
    clock = Clock()
    client, wire, _ = make(*[TransportError("TimeoutError") for _ in range(5)], clock=clock, wall_budget_s=3)
    with pytest.raises(LLMUnavailable):
        client.complete_json("s", "u")
    assert len(wire.calls) < 5  # the budget ran out before the retries did


def test_a_refused_key_is_not_retried_and_nothing_secret_is_in_the_message() -> None:
    client, wire, _ = make(status(401, body=b'{"error": "bad key sk-test-key-value"}'))
    with pytest.raises(LLMUnavailable) as caught:
        client.complete_json("s", "u")
    assert len(wire.calls) == 1 and "401" in str(caught.value)
    assert "sk-test-key-value" not in str(caught.value) and "bad key" not in str(caught.value)


def test_another_client_error_names_the_status_and_not_the_body() -> None:
    client, wire, _ = make(status(404, body=b"model SECRET-BODY not found"))
    with pytest.raises(LLMUnavailable) as caught:
        client.complete_json("s", "u")
    assert len(wire.calls) == 1 and "404" in str(caught.value) and "SECRET-BODY" not in str(caught.value)


def test_the_provider_body_of_a_failed_request_never_reaches_the_message() -> None:
    client, _, _ = make(*[status(500, body=b"internal: SECRET-BODY") for _ in range(5)])
    with pytest.raises(LLMUnavailable) as caught:
        client.complete_json("s", "u")
    assert "SECRET-BODY" not in str(caught.value)


# --- The limiter ------------------------------------------------------------------------------------------------------------


def test_the_limiter_spaces_calls() -> None:
    clock = Clock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)  # one every two seconds
    waits = [limiter.acquire(60) for _ in range(3)]
    assert waits == [0.0, 2.0, 2.0] and clock.slept == [2.0, 2.0]


def test_a_limiter_with_no_limit_never_waits_and_a_wait_over_the_budget_is_refused() -> None:
    clock = Clock()
    assert RateLimiter(0, clock=clock, sleep=clock.sleep).acquire(0) == 0.0 and clock.slept == []
    slow = RateLimiter(1, clock=clock, sleep=clock.sleep)
    slow.acquire(1)
    with pytest.raises(LLMUnavailable):
        slow.acquire(5)  # would wait 60 seconds


@pytest.mark.parametrize("bad", [-1, 1.5, True, "3"])
def test_a_limiter_needs_a_whole_non_negative_rate(bad: Any) -> None:
    with pytest.raises(ValueError):
        RateLimiter(bad)


def test_the_client_goes_through_the_limiter() -> None:
    clock = Clock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
    client, wire, _ = make(ok("{}"), ok("{}"), clock=clock, limiter=limiter)
    client.complete_json("s", "u")
    client.complete_json("s", "u")
    assert clock.slept == [2.0] and len(wire.calls) == 2


def test_shared_limiters_are_one_per_role_endpoint_and_rate() -> None:
    llm.reset_shared_limiters()
    a = llm.shared_limiter("challenger", "https://x.test/v1", 10)
    assert a is llm.shared_limiter("challenger", "https://x.test/v1", 10)
    assert a is not llm.shared_limiter("builder", "https://x.test/v1", 10)
    llm.reset_shared_limiters()


# --- What is never sent -----------------------------------------------------------------------------------------------------


def test_the_token_used_here_really_looks_like_a_secret() -> None:
    assert secretscan.scan(TOKEN)


def test_a_prompt_with_a_secret_shaped_string_is_refused_and_never_leaves() -> None:
    client, wire, _ = make(ok("{}"))
    with pytest.raises(LLMRefused) as caught:
        client.complete_json("s", f"here is my token {TOKEN}")
    assert wire.calls == []
    assert "github_token" in str(caught.value) and TOKEN not in str(caught.value)


def test_a_secret_in_the_system_prompt_is_refused_too() -> None:
    client, wire, _ = make(ok("{}"))
    with pytest.raises(LLMRefused):
        client.complete_json(f"token {TOKEN}", "u")
    assert wire.calls == []


# --- FakeLLM goes through the same code -------------------------------------------------------------------------------------


def test_fake_llm_uses_the_same_parse_nudge_and_secret_scan() -> None:
    fake = FakeLLM(["not json", {"cases": []}], model="fake-x")
    reply = fake.complete_json("s", "u")
    assert (
        reply.nudged and reply.data == {"cases": []} and reply.model == "fake-x" and len(fake.requests) == 2
    )
    with pytest.raises(LLMRefused):
        FakeLLM([{}]).complete_json("s", TOKEN)


def test_fake_llm_raises_scripted_errors_and_fails_loudly_when_the_script_runs_out() -> None:
    with pytest.raises(LLMUnavailable):
        FakeLLM([LLMUnavailable("outage")]).complete_json("s", "u")
    with pytest.raises(AssertionError):
        FakeLLM([]).complete_json("s", "u")


# --- The real transport's mapping -------------------------------------------------------------------------------------------


def test_urllib_transport_returns_http_errors_and_raises_on_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Opener:
        def __init__(self, fail: Exception) -> None:
            self.fail = fail

        def open(self, request: Any, timeout: float) -> Any:
            raise self.fail

    transport = llm.UrllibTransport()
    http_error = urllib.error.HTTPError("https://x.test", 429, "too many", {"Retry-After": "3"}, None)  # type: ignore[arg-type]
    monkeypatch.setattr(llm.urllib.request, "build_opener", lambda *a: Opener(http_error))
    response = transport.post("https://x.test", {}, b"{}", 1.0)
    assert response.status == 429 and response.headers["retry-after"] == "3"
    monkeypatch.setattr(
        llm.urllib.request, "build_opener", lambda *a: Opener(urllib.error.URLError("refused"))
    )
    with pytest.raises(TransportError):
        transport.post("https://x.test", {}, b"{}", 1.0)


def test_a_redirect_is_never_followed() -> None:
    assert llm._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test") is None


# --- Settings ---------------------------------------------------------------------------------------------------------------


def test_no_endpoint_or_no_model_means_no_client() -> None:
    assert client_from_settings(Settings(), "challenger") is None
    assert client_from_settings(Settings(llm_base_url="https://x.test/v1"), "builder") is None
    assert client_from_settings(Settings(llm_base_url="ftp://x.test", llm_model="m"), "builder") is None


def test_the_challenger_falls_back_to_the_builder_and_uses_its_own_values_when_set() -> None:
    llm.reset_shared_limiters()
    fallback = client_from_settings(
        Settings(llm_base_url="https://a.test/v1", llm_model="builder-m", llm_api_key="k1", llm_max_rpm=6),
        "challenger",
    )
    assert fallback is not None and fallback.model == "builder-m" and fallback.base_url == "https://a.test/v1"
    assert fallback._limiter.max_rpm == 6  # no endpoint of its own: the builder's limit applies
    own = client_from_settings(
        Settings(
            llm_base_url="https://a.test/v1",
            llm_model="builder-m",
            challenger_base_url="https://b.test/v1",
            challenger_model="other-m",
            challenger_max_rpm=3,
            llm_max_rpm=6,
            llm_timeout_s=15,
        ),
        "challenger",
    )
    assert own is not None and own.model == "other-m" and own._limiter.max_rpm == 3 and own._timeout_s == 15
    llm.reset_shared_limiters()


def test_the_challenger_with_its_own_endpoint_does_not_inherit_the_builders_limit() -> None:
    settings = Settings(llm_max_rpm=6, challenger_base_url="https://b.test/v1")
    assert settings.effective_challenger_max_rpm == 0


def test_the_same_model_is_noticed_whatever_the_case() -> None:
    assert Settings(llm_model="Model-A", challenger_model="model-a ").challenger_shares_builder_model
    assert Settings(llm_model="m").challenger_shares_builder_model  # the challenger falls back to the builder
    assert not Settings(llm_model="m", challenger_model="n").challenger_shares_builder_model
    assert not Settings().challenger_shares_builder_model
    assert not Settings(challenger_model="n").challenger_shares_builder_model


def test_new_settings_are_validated() -> None:
    for bad in (
        {"llm_max_rpm": -1},
        {"challenger_max_rpm": -2},
        {"llm_timeout_s": 0},
        {"llm_timeout_s": 601},
    ):
        with pytest.raises(ValueError):
            Settings(**bad)
