"""An OpenAI-compatible chat client that asks for one JSON object, and a scripted stand-in for tests.

It speaks the chat-completions protocol, so it works with any provider that does: a hosted one, Gemini through its OpenAI-compatible
endpoint, or an open model behind a local server. By default it sends only `model` and `messages`, no vendor-specific parameter
(`json_mode="native"` adds the standard `response_format` and falls back to the prompt-only mode when the server rejects it).

Properties, each proven by `tests/test_llm_client.py`:

* **Nothing secret-shaped leaves the process.** Every outgoing prompt goes through `verify.secretscan.scan`; a hit raises `LLMRefused`
  naming the pattern, never the text, before any request is made.
* **Rate limits and outages end in `LLMUnavailable`.** A limiter spaces requests (`max_rpm`, 0 means unlimited). HTTP 429, 5xx and
  connection errors are retried at most four times with exponential backoff and jitter, `Retry-After` is honoured, and everything
  happens inside one wall-clock budget. A gate turns `LLMUnavailable` into `skipped`: an outage says nothing about a tool.
* **Replies are parsed, never repaired.** Code fences and trailing commas are tolerated; a reply that was cut off is not completed.
  One parse failure earns one specific nudge; a second raises `LLMBadReply`.
* **Nothing the provider says is shown.** Error messages carry a status code and a fixed phrase, never the response body or the key.

Time and randomness are injected (`clock`, `sleep`, `rng`, `wall_time`) and the network is a `Transport`, so no test sleeps or connects.
`FakeLLM` goes through the same parse, nudge and secret-scan code as the real client; only the bytes on the wire are replaced.
"""

from __future__ import annotations

import email.utils
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from toolforge import textsafety
from toolforge.errors import LLMBadReply, LLMRefused, LLMUnavailable
from toolforge.verify import secretscan

if TYPE_CHECKING:
    from toolforge.config import Settings

MAX_RETRIES = 4  # retries after the first attempt, so at most five requests for one question
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
DEFAULT_WALL_BUDGET_S = 120.0  # rate-limit waits, backoff, every request and the nudge together
MAX_RESPONSE_BYTES = 1_000_000
MAX_NUDGE_ECHO_CHARS = 2000

JsonMode = Literal["prompt", "native"]


# --- The wire ------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class TransportError(Exception):
    """The request did not get an answer (connection refused, timeout, TLS failure). `retryable` is always true for those."""


class Transport(Protocol):
    def post(self, url: str, headers: dict[str, str], body: bytes, timeout_s: float) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would send the bearer token to wherever the server points. A provider that redirects is misconfigured."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


class UrllibTransport:
    """The real transport: the standard library, no redirects, a response size cap. HTTP errors are returned, not raised."""

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout_s: float) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 (scheme is checked)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout_s) as response:  # noqa: S310
                return HttpResponse(
                    response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES],
                )
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
            except Exception:
                payload = b""
            return HttpResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()}, payload)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TransportError(type(exc).__name__) from exc


# --- Pacing --------------------------------------------------------------------------------------------------------------


class RateLimiter:
    """Spaces requests at least `60 / max_rpm` seconds apart across threads. `max_rpm == 0` never waits.

    A caller reserves its slot under a lock and sleeps outside it, so two verifications running at once share one schedule.
    """

    def __init__(
        self,
        max_rpm: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(max_rpm, bool) or not isinstance(max_rpm, int) or max_rpm < 0:
            raise ValueError("max_rpm is a whole number from 0 (no limit) upwards")
        self.max_rpm = max_rpm
        self._interval = 60.0 / max_rpm if max_rpm else 0.0
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self, max_wait_s: float) -> float:
        """Wait for a slot and return how long that took. Raises `LLMUnavailable` instead when the wait would exceed `max_wait_s`."""
        if not self._interval:
            return 0.0
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            wait = start - now
            if wait > max_wait_s:
                raise LLMUnavailable(
                    f"the request limit of {self.max_rpm} per minute would make this request wait {wait:.0f} seconds",
                    hint="raise TOOLFORGE_LLM_MAX_RPM (or TOOLFORGE_CHALLENGER_MAX_RPM), or retry later",
                )
            self._next = start + self._interval
        if wait > 0:
            self._sleep(wait)
        return wait


_LIMITERS: dict[tuple[str, str, int], RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def shared_limiter(role: str, base_url: str, max_rpm: int) -> RateLimiter:
    """One limiter per role, endpoint and limit for the whole process, so concurrent verifications cannot each spend the full rate."""
    key = (role, base_url, max_rpm)
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = _LIMITERS[key] = RateLimiter(max_rpm)
        return limiter


def reset_shared_limiters() -> None:
    """For tests: forget every shared limiter."""
    with _LIMITERS_LOCK:
        _LIMITERS.clear()


# --- Reading a reply -----------------------------------------------------------------------------------------------------


class ReplyProblem(Exception):
    """A reply that is not one usable JSON object. `reason` is a fixed phrase that is safe to send back to the model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_FENCE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def _strip_trailing_commas(text: str) -> str:
    """Remove a comma that is followed only by whitespace and a closing bracket, outside strings. Nothing else is changed."""
    out: list[str] = []
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            rest = text[index + 1 :].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        out.append(char)
    return "".join(out)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def parse_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in a model's reply. Tolerates a code fence, prose around the object and trailing commas.

    A reply that stops before the object closes is a `ReplyProblem`: truncated JSON is never completed, because a guessed ending is
    a guessed answer. `NaN` and `Infinity` are refused. The reason is one of a few fixed phrases.
    """
    if not isinstance(text, str) or not text.strip():
        raise ReplyProblem("the reply was empty")
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    saw_object = False
    for candidate in candidates:
        start = candidate.find("{")
        if start == -1:
            continue
        saw_object = True
        # Only the first `{` is tried: when that object is cut off, a later `{` is an inner fragment, not the answer.
        for attempt in (candidate[start:], _strip_trailing_commas(candidate[start:])):
            try:
                value, _ = json.JSONDecoder(parse_constant=_reject_constant).raw_decode(attempt)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict):
                return value
    if saw_object:
        raise ReplyProblem(
            "no complete JSON object could be read from the reply; it may have been cut off or is not valid JSON"
        )
    raise ReplyProblem("the reply contained no JSON object")


# --- Results -------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatReply:
    """What one request returned. Token counts are None when the provider gave none: not measured, never zero."""

    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    attempts: int
    finish_reason: str | None = None


@dataclass(frozen=True)
class JsonReply:
    """A parsed answer and what it cost. `requests` is 2 when a nudge was needed."""

    data: dict[str, Any]
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    requests: int
    nudged: bool = False


def _sum(values: Sequence[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


class ReplyCheck(Protocol):
    def __call__(self, data: dict[str, Any]) -> None:
        """Raise `ReplyProblem` when `data` is a JSON object but not the one that was asked for."""


Messages = list[dict[str, str]]


class JSONChatClient:
    """Everything except the wire: scanning, parsing, the nudge and the bookkeeping. `chat` is what subclasses provide."""

    model: str = ""
    json_mode: JsonMode = "prompt"

    def chat(
        self, messages: Messages, *, deadline: float | None = None
    ) -> ChatReply:  # pragma: no cover - interface
        raise NotImplementedError

    def _now(self) -> float:
        return time.monotonic()

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        schema_hint: str = "",
        check: ReplyCheck | None = None,
    ) -> JsonReply:
        """Ask for one JSON object. `schema_hint` is added to the system prompt (it is what \"describe the schema in the prompt\" means).

        `check` may reject an object that parses but has the wrong shape; that counts as a parse failure and earns the same single
        nudge. Raises `LLMRefused` before any request when the prompt holds a secret-shaped string, `LLMUnavailable` when the provider
        cannot be used, and `LLMBadReply` when two replies in a row were not usable.
        """
        instruction = "Reply with exactly one JSON object and nothing else: no prose and no code fences."
        if schema_hint:
            instruction = f"{instruction}\n\nThe object must follow this description:\n{schema_hint}"
        messages: Messages = [
            {"role": "system", "content": f"{system.rstrip()}\n\n{instruction}"},
            {"role": "user", "content": user},
        ]
        replies: list[ChatReply] = []
        reason = ""
        for turn in range(2):
            reply = self.chat(messages)
            replies.append(reply)
            try:
                data = parse_json_object(reply.text)
                if reply.finish_reason == "length":
                    raise ReplyProblem(
                        "the reply was cut off by the length limit before the JSON object ended"
                    )
                if check is not None:
                    check(data)
            except ReplyProblem as problem:
                reason = problem.reason
                if turn == 0:
                    messages = [
                        *messages,
                        {
                            "role": "assistant",
                            "content": textsafety.sanitize(reply.text, max_chars=MAX_NUDGE_ECHO_CHARS),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"That reply could not be used: {reason}. Reply again with only the complete JSON object, "
                                "no prose, no code fences, and keep it short enough to finish."
                            ),
                        },
                    ]
                continue
            return JsonReply(
                data,
                replies[-1].model,
                _sum([r.prompt_tokens for r in replies]),
                _sum([r.completion_tokens for r in replies]),
                sum(r.latency_ms for r in replies),
                len(replies),
                nudged=len(replies) > 1,
            )
        raise LLMBadReply(
            f"the model did not return a usable JSON object, even after one nudge ({reason})",
            hint="try a different or larger model for the challenger",
        )


def refuse_secrets(messages: Messages) -> None:
    """Raise `LLMRefused` when any outgoing text looks like a credential. Names the patterns, never the text."""
    findings = secretscan.scan("\n".join(m.get("content", "") for m in messages), "prompt")
    if findings:
        names = sorted({f.pattern for f in findings})
        raise LLMRefused(
            f"the prompt was not sent because it contains secret-shaped text ({', '.join(names)})",
            hint="remove the credential from the tool's files; nothing was sent to the model provider",
        )


class LLMClient(JSONChatClient):
    """The real client. Construct with `client_from_settings` unless a test needs the injected clock or transport."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        timeout_s: float = 60.0,
        limiter: RateLimiter | None = None,
        transport: Transport | None = None,
        json_mode: JsonMode = "prompt",
        wall_budget_s: float = DEFAULT_WALL_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(
                "base_url must be an http or https address without a user name or password in it"
            )
        if not model.strip():
            raise ValueError("a model id is required")
        self.base_url = base_url.strip()
        self.model = model.strip()
        self.json_mode = json_mode
        self._api_key = api_key or None
        self._timeout_s = float(timeout_s)
        self._limiter = limiter or RateLimiter(0)
        self._transport = transport or UrllibTransport()
        self.wall_budget_s = float(wall_budget_s)
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.Random()  # noqa: S311 (jitter, not security)
        self._wall_time = wall_time

    def __repr__(self) -> str:
        return f"LLMClient(base_url={self.base_url!r}, model={self.model!r}, api_key={'***' if self._api_key else None})"

    def _now(self) -> float:
        return self._clock()

    # -- one question, with retries ------------------------------------------------------------------------------------

    @property
    def _url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, messages: Messages, *, native: bool) -> bytes:
        document: dict[str, Any] = {"model": self.model, "messages": messages}
        if native:
            document["response_format"] = {"type": "json_object"}
        return json.dumps(document).encode("utf-8")

    def _retry_after(self, headers: dict[str, str]) -> float | None:
        raw = headers.get("retry-after", "").strip()
        if not raw:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            seconds = when.timestamp() - self._wall_time()
        return max(0.0, seconds) if seconds == seconds else None  # NaN is not a wait

    def chat(self, messages: Messages, *, deadline: float | None = None) -> ChatReply:
        refuse_secrets(messages)
        began = self._clock()
        deadline = deadline if deadline is not None else began + self.wall_budget_s
        native = self.json_mode == "native"
        attempts = 0
        last = "no attempt was made"
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise LLMUnavailable(
                    f"the model provider did not answer within {self.wall_budget_s:.0f} seconds ({last})",
                    hint="check the provider's status, or try again later",
                )
            self._limiter.acquire(max(0.0, remaining))
            attempts += 1
            started = self._clock()
            wait: float | None = None
            try:
                response = self._transport.post(
                    self._url,
                    self._headers(),
                    self._body(messages, native=native),
                    min(self._timeout_s, max(1.0, deadline - self._clock())),
                )
            except TransportError as exc:
                last = f"connection problem: {exc}"
            else:
                status = response.status
                if status == 200:
                    return self._read(response, attempts, started)
                if native and status in (400, 422):
                    native = False  # the server does not know `response_format`; ask in the prompt instead
                    self.json_mode = "prompt"
                    attempts -= 1
                    continue
                if status in (401, 403):
                    raise LLMUnavailable(
                        f"the model provider refused the credentials (HTTP {status})",
                        hint="check the API key and the model id in .env",
                    )
                if status == 429 or 500 <= status < 600:
                    last = f"HTTP {status}"
                    wait = self._retry_after(response.headers)
                else:
                    raise LLMUnavailable(
                        f"the model provider rejected the request (HTTP {status})",
                        hint="check TOOLFORGE_LLM_BASE_URL and the model id; the base URL should end where `/chat/completions` begins",
                    )
            if attempts > MAX_RETRIES:
                raise LLMUnavailable(
                    f"the model provider stayed unavailable after {attempts} requests ({last})",
                    hint="try again later, lower TOOLFORGE_LLM_MAX_RPM, or use another provider",
                )
            delay = (
                wait
                if wait is not None
                else min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempts - 1)) * (0.5 + self._rng.random() / 2)
            )
            if delay > deadline - self._clock():
                raise LLMUnavailable(
                    f"the model provider asked for a wait of {delay:.0f} seconds, more than this request may spend ({last})",
                    hint="try again later",
                )
            self._sleep(delay)

    def _read(self, response: HttpResponse, attempts: int, started: float) -> ChatReply:
        try:
            document = json.loads(response.body.decode("utf-8", "replace"))
            choice = document["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            return ChatReply(
                "", self.model, None, None, int((self._clock() - started) * 1000), attempts, None
            )
        if isinstance(content, list):  # some providers answer in parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = document.get("usage") if isinstance(document, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        reported = document.get("model") if isinstance(document, dict) else None
        return ChatReply(
            content if isinstance(content, str) else "",
            reported if isinstance(reported, str) and reported else self.model,
            _token(usage.get("prompt_tokens")),
            _token(usage.get("completion_tokens")),
            int((self._clock() - started) * 1000),
            attempts,
            finish if isinstance(finish, str) else None,
        )


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# --- A scripted stand-in --------------------------------------------------------------------------------------------------


class FakeLLM(JSONChatClient):
    """Answers from a script, through the same scanning, parsing and nudging as the real client.

    `replies` are consumed one per request: a `str` is the model's text, a `dict` is dumped as JSON, an exception instance is
    raised (for example `LLMUnavailable("outage")`). Every request's messages are kept in `requests` for assertions; running out of
    replies fails the test loudly instead of inventing an answer.
    """

    def __init__(self, replies: Sequence[Any] = (), model: str = "fake-model") -> None:
        self.replies: list[Any] = list(replies)
        self.model = model
        self.requests: list[Messages] = []

    def chat(self, messages: Messages, *, deadline: float | None = None) -> ChatReply:
        refuse_secrets(messages)
        self.requests.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError("FakeLLM was asked for more replies than the test scripted")
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return ChatReply(text, self.model, len(str(messages)) // 4, len(text) // 4, 1, 1, "stop")


# --- Building a client from settings -------------------------------------------------------------------------------------


def client_from_settings(settings: Settings, role: Literal["builder", "challenger"]) -> LLMClient | None:
    """A client for the builder or the challenger, or None when no endpoint and model are configured for it.

    The challenger falls back to the builder's endpoint, model and key (`Settings.effective_challenger_*`). An unusable value
    (a base URL that is not http or https) is also None: the caller says \"no model configured\", and `toolforge doctor` says why.
    """
    if role == "challenger":
        base_url = settings.effective_challenger_base_url
        model = settings.effective_challenger_model
        key = settings.effective_challenger_api_key
        rpm = settings.effective_challenger_max_rpm
    else:
        base_url, model, key, rpm = (
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_api_key,
            settings.llm_max_rpm,
        )
    if not base_url or not model:
        return None
    try:
        return LLMClient(
            base_url,
            model,
            key.get_secret_value() if key is not None else None,
            timeout_s=settings.llm_timeout_s,
            limiter=shared_limiter(role, base_url, rpm),
        )
    except ValueError:
        return None


__all__ = [
    "DEFAULT_WALL_BUDGET_S",
    "MAX_RETRIES",
    "ChatReply",
    "FakeLLM",
    "HttpResponse",
    "JSONChatClient",
    "JsonReply",
    "LLMClient",
    "RateLimiter",
    "ReplyProblem",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "client_from_settings",
    "parse_json_object",
    "refuse_secrets",
    "reset_shared_limiters",
    "shared_limiter",
]
