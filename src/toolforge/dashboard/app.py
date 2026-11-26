"""The operator dashboard (P17): one FastAPI app and one static page, for a person.

It sits on the same files as the command line, in its own process. It reads the registry and the ledgers through the same code the
CLI uses, and its writes (approve, deny, promote, quarantine, release, rollback, close a session) are the same functions the CLI calls
(`services.operator_actions`), so the same refusals apply and the same ledger events are written. The MCP server does not import any of
this and an agent has no route to it.

**Network.** The listener is `127.0.0.1` unless told otherwise, and any other host needs `TOOLFORGE_DASHBOARD_TOKEN` (the process
refuses to start without it). `NetGuard` (P07) checks `Host` and `Origin` on every request and the bearer token on everything under
`/api`; the page itself (`/`, `/static/*`) carries no server data and loads without a token, because a browser cannot attach a bearer
header to a page load. The routes under `/api` check the token again themselves. There are no cookies: the token travels only in an
`Authorization` header, so a page on another origin has nothing to ride on, and a state-changing request must also be `application/json`.

**What it shows.** Tool-authored text is cleaned (see `views.py`) and the page writes it with `textContent` only. The page has no inline
script, and its content-security policy allows scripts from itself only. A report that was never stored, a success rate that was never
measured and a ledger that cannot be verified are shown as gaps, never as zeros or ticks.

**What it does not do.** It never starts a sandbox. Approving a version whose manifest has live examples needs one, so the dashboard
refuses that approval and names the command (`toolforge approvals approve`). Attack and benchmark runs are command-line tools too; their
stored result files are shown.

Proven by `tests/test_dashboard_*.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from toolforge import __version__, cli_sessions
from toolforge import cli_run as _cli_run
from toolforge.config import Settings
from toolforge.dashboard import results, views
from toolforge.dashboard.events import EventFeed
from toolforge.errors import IllegalTransition, LedgerError, SandboxUnavailable, ToolForgeError
from toolforge.netguard import NetGuard, bearer_matches, is_loopback, token_text
from toolforge.registry import Registry
from toolforge.server.fences import clean_text
from toolforge.services import approvals, operator_actions

log = logging.getLogger(__name__)

API = "/api"
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
MAX_REASON = 500
SSE_KEEPALIVE_S = 15.0
SESSION_ID_CHARS = 40

# The page loads scripts and styles from itself only and talks to itself only. Inline style is allowed because the Blast Radius page
# (drawn by MiniLocker, shown in a sandboxed frame) styles itself inline; inline script is not allowed anywhere.
PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
API_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=MAX_REASON)
    version: str | None = Field(default=None, max_length=80)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    reason: str = Field(min_length=1, max_length=MAX_REASON)


def _error(status: int, code: str, message: str, hint: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if hint:
        body["error"]["hint"] = hint
    return JSONResponse(
        body, status_code=status, headers={"WWW-Authenticate": "Bearer"} if status == 401 else None
    )


def status_for(exc: ToolForgeError) -> int:
    """The HTTP status an operator-action refusal is answered with."""
    if str(exc.code).startswith("unknown_"):
        return 404
    if isinstance(exc, SandboxUnavailable):
        return 503
    if isinstance(exc, IllegalTransition):
        return 409
    if isinstance(exc, LedgerError):
        return 500
    return 400


@dataclass
class Dashboard:
    """The app, the guarded app to serve, and what they use. `close()` closes the registry only if the dashboard opened it."""

    settings: Settings
    registry: Registry
    app: FastAPI
    guarded: NetGuard
    _owns_registry: bool = False

    def close(self) -> None:
        if self._owns_registry:
            self.registry.close()


def _static(name: str) -> bytes:
    return (resources.files("toolforge.dashboard") / "static" / name).read_bytes()


def build_dashboard(
    settings: Settings,
    *,
    registry: Registry | None = None,
    runs_dir: str | Path = "runs",
    ledger_factory: Callable[[str | None, str], Any] | None = None,
    event_interval_s: float = 1.0,
    session_close_wait_s: float = 8.0,
) -> Dashboard:
    """Build the dashboard. Raises `ConfigError` (before anything listens) for a non-loopback host without a token."""
    owns = registry is None
    reg = registry if registry is not None else Registry.from_settings(settings)
    factory = ledger_factory or _cli_run.make_ledger
    token = token_text(settings.dashboard_token)
    feed = EventFeed(settings, reg)
    runs = Path(runs_dir)

    app = FastAPI(title="ToolForge dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    # -- errors, headers --------------------------------------------------------------------------------------------------

    @app.exception_handler(ToolForgeError)
    async def _tf_error(_: Request, exc: ToolForgeError) -> JSONResponse:
        return _error(
            status_for(exc),
            str(exc.code),
            clean_text(exc.message, 600),
            clean_text(exc.hint, 600) if exc.hint else None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code") or {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        message = detail.get("message") or (exc.detail if isinstance(exc.detail, str) else "request refused")
        return _error(exc.status_code, str(code), str(message), detail.get("hint"))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("dashboard request failed")
        return _error(
            500, "internal_error", "the dashboard hit an unexpected error; the details are in its log"
        )

    @app.middleware("http")
    async def _headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        for key, value in HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith(API + "/") or request.url.path == API:
            response.headers.setdefault("Content-Security-Policy", API_CSP)
        else:
            response.headers.setdefault("Content-Security-Policy", PAGE_CSP)
        return response

    # -- auth: checked here as well as by NetGuard ------------------------------------------------------------------------

    def require_token(request: Request) -> None:
        if token is None:
            return
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not bearer_matches(values[0], token):
            raise StarletteHTTPException(
                401,
                {
                    "code": "unauthorized",
                    "message": "a bearer token is required",
                    "hint": "send Authorization: Bearer <TOOLFORGE_DASHBOARD_TOKEN>",
                },
            )

    async def require_json(request: Request) -> None:
        """A state-changing request must be JSON. A cross-site form or `text/plain` post is then refused before it reaches a handler."""
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise StarletteHTTPException(
                415, {"code": "json_required", "message": "send the body as application/json"}
            )

    read = APIRouter(prefix=API, dependencies=[Depends(require_token)])
    write = APIRouter(prefix=API, dependencies=[Depends(require_token), Depends(require_json)])

    def need_version(name: str, version: str) -> Any:
        record = reg.find_version(name, version)
        if record is None:
            raise StarletteHTTPException(
                404,
                {
                    "code": "unknown_version",
                    "message": f"no version {clean_text(name, 80)}@{clean_text(version, 80)}",
                },
            )
        return record

    def expire() -> None:
        """Deny overdue approval requests, exactly as every `toolforge approvals` command does first. It can only say no."""
        approvals.expire_overdue(settings, reg, ledger_factory=factory)

    # -- read -------------------------------------------------------------------------------------------------------------

    @read.get("/meta")
    def meta() -> dict[str, Any]:
        return {**views.meta(__version__), "token_required": token is not None}

    @read.get("/tools")
    def tools(
        q: str = "", status: str = "", limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        return views.tool_list(reg, query=q, status=status, limit=limit, offset=offset)

    @read.get("/tools/{name}")
    def tool(name: str) -> dict[str, Any]:
        found = views.tool_detail(reg, name)
        if found is None:
            raise StarletteHTTPException(
                404, {"code": "unknown_tool", "message": f"no tool {clean_text(name, 80)}"}
            )
        return found

    @read.get("/versions/{name}/{version}")
    def version(name: str, version: str) -> dict[str, Any]:
        return views.version_detail(reg, need_version(name, version))

    @read.get("/versions/{name}/{version}/calls")
    def calls(name: str, version: str, limit: int | None = None, before: int | None = None) -> dict[str, Any]:
        return views.calls_page(reg, need_version(name, version), limit=limit, before=before)

    @read.get("/versions/{name}/{version}/report", response_model=None)
    def report(name: str, version: str, format: Literal["json", "html"] = "json") -> Any:
        record = need_version(name, version)
        if format == "json":
            return views.stored_report(settings, record)
        try:
            page = views.report_html(settings, record)
        except SandboxUnavailable as exc:
            raise StarletteHTTPException(
                503, {"code": exc.code, "message": clean_text(exc.message, 300)}
            ) from None
        if page is None:
            raise StarletteHTTPException(
                404,
                {
                    "code": "no_report",
                    "message": "no Blast Radius report is stored for this version",
                    "hint": "run `toolforge report <name@version>` to make one",
                },
            )
        # The page is MiniLocker's own rendering and escapes what it prints; this policy makes it inert anyway.
        return Response(
            page,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                "X-Frame-Options": "DENY",
            },
        )

    @read.get("/ledger/{name}/{version}/verify")
    def ledger_verify(
        name: str, version: str, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        return views.ledger_verify(settings, need_version(name, version), limit=limit, offset=offset)

    @read.get("/approvals")
    def approvals_list(status: Literal["pending", "approved", "denied", "all"] = "pending") -> dict[str, Any]:
        expire()
        rows = reg.list_approvals(None if status == "all" else status)
        rows = sorted(rows, key=lambda a: a.id, reverse=True)[:200]
        return {"approvals": [views.approval_view(reg, a) for a in rows]}

    @read.get("/approvals/{approval_id}")
    def approval_one(approval_id: int) -> dict[str, Any]:
        expire()
        found = reg.get_approval(approval_id)
        if found is None:
            raise StarletteHTTPException(
                404, {"code": "unknown_approval", "message": f"no approval request with id {approval_id}"}
            )
        return views.approval_view(reg, found, with_text=True)

    @read.get("/sessions")
    def sessions(state: Literal["open", "all"] = "open", limit: int | None = None) -> dict[str, Any]:
        return views.sessions_list(reg, state=state, limit=limit)

    @read.get("/attacks")
    def attacks(name: str | None = Query(default=None, max_length=200)) -> dict[str, Any]:
        return results.stored_results(runs, "attacks", name)

    @read.get("/bench")
    def bench(name: str | None = Query(default=None, max_length=200)) -> dict[str, Any]:
        return results.stored_results(runs, "bench", name)

    @read.get("/events")
    async def events(
        request: Request, replay: int = 0, max_events: int | None = Query(default=None, ge=1, le=1000)
    ) -> StreamingResponse:
        cursor = await anyio.to_thread.run_sync(feed.cursor, replay)

        async def stream() -> AsyncIterator[str]:
            yield "event: ready\ndata: " + json.dumps({"toolforge_version": __version__}) + "\n\n"
            sent = 0
            idle = 0.0
            while max_events is None or sent < max_events:
                if await request.is_disconnected():
                    return
                batch = await anyio.to_thread.run_sync(feed.poll, cursor)
                for event in batch:
                    yield f"id: {event['id']}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
                    sent += 1
                    if max_events is not None and sent >= max_events:
                        return
                if batch:
                    idle = 0.0
                else:
                    idle += event_interval_s
                    if idle >= SSE_KEEPALIVE_S:
                        idle = 0.0
                        yield ": keepalive\n\n"
                await asyncio.sleep(event_interval_s)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- write: the same functions the command line calls -----------------------------------------------------------------

    def done(
        label: str, text: str, *, written: bool = True, warning: LedgerError | None = None, **extra: Any
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": True,
            "label": label,
            "text": clean_text(text, 2000),
            "ledger_written": written and warning is None,
            **extra,
        }
        if warning is not None:
            out["warning"] = clean_text(str(warning), 600)
        elif not written:
            out["warning"] = (
                "the change is made, but its ledger event could not be written: check that the ledger folder under $TOOLFORGE_HOME is writable"
            )
        return out

    def run_action(call: Callable[[], Any]) -> tuple[Any, LedgerError | None]:
        """Run an operator action. A `LedgerError` from one always means the registry already moved and only the history could not be
        written (the action says so in its message), so the page gets `ok: true` with a warning, never a failure to retry."""
        try:
            return call(), None
        except LedgerError as exc:
            return None, exc

    @write.post("/approvals/{approval_id}")
    def decide(approval_id: int, body: DecisionBody) -> dict[str, Any]:
        # No tool runner factory: a live example needs a sandbox, and a web request does not start one. decide() then refuses with
        # `smoke_needs_sandbox` for such a version and changes nothing.
        result, warn = run_action(
            lambda: operator_actions.decide(
                settings, reg, approval_id, body.decision, body.reason, ledger_factory=factory
            )
        )
        if result is None:
            return done("approval", f"approval {approval_id}: the decision was recorded", warning=warn)
        return done(
            result.label,
            result.text(),
            written=bool(result.ledger_files),
            warning=warn,
            outcome=result.outcome,
            status=result.to_status,
        )

    @write.post("/versions/{name}/{version}/promote")
    def promote(name: str, version: str, body: ReasonBody) -> dict[str, Any]:
        need_version(name, version)
        result, warn = run_action(
            lambda: operator_actions.promote(
                settings, reg, f"{name}@{version}", body.reason, ledger_factory=factory
            )
        )
        if result is None:
            return done(f"{name}@{version}", "promoted", warning=warn)
        return done(
            result.label, result.text(), written=bool(result.ledger_files), warning=warn, status="active"
        )

    @write.post("/tools/{name}/quarantine")
    def quarantine(name: str, body: ReasonBody) -> dict[str, Any]:
        target = body.version
        if target is None:
            active = reg.active_version(name)
            if active is None:
                raise IllegalTransition(
                    f"{clean_text(name, 80)} has no active version to quarantine",
                    code="not_quarantinable",
                    hint="name the version",
                )
            target = active.version
        need_version(name, target)
        result, warn = run_action(
            lambda: operator_actions.quarantine(
                settings, reg, f"{name}@{target}", body.reason, ledger_factory=factory
            )
        )
        if result is None:
            return done(f"{name}@{target}", "quarantined", warning=warn)
        return done(
            result.label, result.text(), written=bool(result.ledger_files), warning=warn, status="quarantined"
        )

    @write.post("/tools/{name}/release")
    def release(name: str, body: ReasonBody) -> dict[str, Any]:
        if not body.version:
            raise IllegalTransition(
                "say which version to release",
                code="version_required",
                hint='send {"version": "1.0.0", "reason": "..."}',
            )
        need_version(name, body.version)
        result, warn = run_action(
            lambda: operator_actions.release(
                settings, reg, f"{name}@{body.version}", body.reason, ledger_factory=factory
            )
        )
        if result is None:
            return done(f"{name}@{body.version}", "released", warning=warn)
        return done(
            result.label, result.text(), written=bool(result.ledger_files), warning=warn, status="active"
        )

    @write.post("/tools/{name}/rollback")
    def rollback(name: str, body: ReasonBody) -> dict[str, Any]:
        if not body.version:
            raise IllegalTransition(
                "say which version to go back to",
                code="version_required",
                hint='send {"version": "1.0.0", "reason": "..."}',
            )
        need_version(name, body.version)
        result, warn = run_action(
            lambda: operator_actions.rollback(
                settings, reg, name, body.version or "", body.reason, ledger_factory=factory
            )
        )
        if result is None:
            return done(f"{name}@{body.version}", "rolled back", warning=warn)
        return done(
            result.label, result.text(), written=bool(result.ledger_files), warning=warn, status="active"
        )

    @write.post("/sessions/{session_id}/close")
    def close_session(session_id: str) -> dict[str, Any]:
        public = session_id.partition(".")[0]  # only the public half is ever taken from a client
        if not public or len(public) > SESSION_ID_CHARS:
            raise StarletteHTTPException(404, {"code": "unknown_session", "message": "no such session"})
        closed, message = cli_sessions.close_one(settings, reg, public, wait_s=session_close_wait_s)
        return {"ok": closed, "closed": closed, "text": clean_text(message, 600)}

    app.include_router(read)
    app.include_router(write)

    # -- the page ---------------------------------------------------------------------------------------------------------

    def asset(name: str) -> Response:
        return Response(_static(name), media_type=STATIC_FILES[name])

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        return asset("index.html")

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str) -> Response:
        if name not in STATIC_FILES or name == "index.html":
            raise StarletteHTTPException(404, {"code": "not_found", "message": "no such file"})
        return asset(name)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return PlainTextResponse("", status_code=204)

    guarded = NetGuard(
        app,
        bind_host=settings.dashboard_host,
        port=settings.dashboard_port,
        token=settings.dashboard_token,
        allowed_hosts=settings.dashboard_allowed_hosts,
        allowed_origins=settings.dashboard_allowed_origins,
        token_paths=(API,),
        env_var="TOOLFORGE_DASHBOARD_TOKEN",
        what="the dashboard",
    )
    return Dashboard(settings, reg, app, guarded, owns)


async def serve(dash: Dashboard) -> None:
    import uvicorn

    settings = dash.settings
    config = uvicorn.Config(
        dash.guarded,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=3,  # an open event stream must not keep the process alive after Ctrl-C
    )
    await uvicorn.Server(config).serve()


def describe_listener(settings: Settings) -> str:
    """One line for the operator at start-up. Never contains the token."""
    secured = (
        "bearer token required for /api"
        if token_text(settings.dashboard_token) is not None
        else "loopback only, no token"
    )
    where = f"http://{settings.dashboard_host}:{settings.dashboard_port}/"
    return f"toolforge dashboard listening on {where} ({secured}{'' if is_loopback(settings.dashboard_host) else ', not loopback'})"


__all__ = ["API", "Dashboard", "build_dashboard", "describe_listener", "serve", "status_for"]
