"""Runs a browser plan in MiniLocker's browser sandbox (P14b). The only module that touches `minilocker.sandbox.browser`.

A plan is data (`adapters/browser_plan.py`); this adapter is the host side that executes it. For one run it takes the egress proxy
from the pool with the plan's approved hosts, starts a `BrowserSandbox` on it (Chromium in a hardened container: non-root, read-only
root filesystem, no capabilities, a network whose only neighbour is the proxy), runs the steps, writes what the proxy saw to the call's
ledger in the same `egress.*` form the code sandbox uses, then destroys the container and gives the proxy back. Page text comes back as
data and is cleaned by `run_steps`; nothing a page says reaches a decision here.

`audit()` is what gate G6 judges for a plan: it starts a browser sandbox with **no network**, reads back the container's hardening as
Docker reports it, and destroys it. It does not run the plan (a plan has no approved hosts before approval, so nothing may be visited
yet), and it does not judge: the gate applies the same invariants as for a code sandbox.

If MiniLocker, Docker or the browser image is missing, both methods say so and never pretend a plan ran.

Known limits. The egress proxy has one allowlist for every sandbox and passes subdomains of an allowed host, so the *browser* can load a
page resource from a host approved for another tool; `run_steps` stops a plan whose page is on a host the plan was not approved for, after
the browser has already fetched it. The browser image is MiniLocker's and is built by hand (`docs/browser-plans.md`).

Proven with a scripted browser in `tests/test_browser_adapter.py`; the real containers need Docker and the `minilocker-browser:latest`
image (`tests/docker/test_browser_plans.py`).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from toolforge import textsafety
from toolforge.adapters import browser_plan
from toolforge.config import Settings
from toolforge.errors import SandboxUnavailable
from toolforge.ledger import ToolforgeLedger
from toolforge.sandbox import _ml_path

log = logging.getLogger(__name__)

MIN_TIMEOUT_S = 5
# MiniLocker's own default; MINILOCKER_BROWSER_IMAGE changes it.
BROWSER_IMAGE_DEFAULT = "minilocker-browser:latest"


@dataclass(frozen=True)
class PlanOutcome:
    """What one run of a plan produced. `egress_attempts` and `blocked` are None when the proxy could not be read (not measured).

    `error_type` is None on success, else one of `BrowserUnavailable` (nothing ran: the operator's problem), `PlanFailed` (a step or
    the page did not do what the plan expects), `HostLeft` (the page moved to a host that is not approved), `Timeout` or
    `SandboxCrashed`.
    """

    ok: bool
    fields: dict[str, Any] | None = None
    error_type: str | None = None
    message: str = ""
    steps_run: int = 0
    pages: tuple[str, ...] = ()
    screenshots: int = 0
    egress_attempts: int | None = None
    blocked: int | None = None
    duration_ms: int = 0
    cleanup_ok: bool = True


@dataclass(frozen=True)
class BrowserAudit:
    """`hardening` is what Docker reports for a browser container; None means nothing was measured, and `reason` says why."""

    hardening: dict[str, Any] | None
    reason: str = ""


class PlanRunner(Protocol):
    """What the tool runner and gate G6 need from a browser: tests pass a scripted one, production passes `BrowserAdapter`."""

    @property
    def image(self) -> str: ...

    def available(self) -> tuple[bool, str]: ...

    def run(
        self,
        steps: Sequence[dict[str, Any]],
        *,
        hosts: tuple[str, ...],
        timeout_s: int,
        ledger_path: str | None,
        label: str,
    ) -> PlanOutcome: ...

    def audit(self, *, ledger_path: str | None, label: str) -> BrowserAudit: ...


class _Clocked:
    """A browser that refuses another call once the run's time is up, so a slow page cannot outlast the tool's timeout."""

    def __init__(self, box: Any, deadline: float, clock: Callable[[], float]) -> None:
        self._box, self._deadline, self._clock = box, deadline, clock

    def _expired(self) -> browser_plan.PlanFailure:
        return browser_plan.PlanFailure("timeout", "the plan ran out of time")

    def call(self, action: str, params: dict[str, Any], timeout_s: int = 40) -> dict[str, Any]:
        left = self._deadline - self._clock()
        if left <= 0:
            raise self._expired()
        reply = self._box.call(action, params, timeout_s=max(1, min(timeout_s, int(left) + 1)))
        if self._clock() >= self._deadline and not (isinstance(reply, dict) and reply.get("ok")):
            raise self._expired()
        return reply


_FAILURES = {"host": "HostLeft", "browser": "PlanFailed", "page": "PlanFailed", "timeout": "Timeout"}


class BrowserAdapter:
    """`PlanRunner` on MiniLocker. `pool` is the `EgressPool` the code sandboxes use too (there is one proxy per process)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        pool: Any = None,
        browser_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._factory = browser_factory
        self._clock = clock

    # -- is there a browser ----------------------------------------------------------------------------------------------

    def _module(self) -> Any:
        return _ml_path.load("minilocker.sandbox.browser", self._settings)

    @property
    def image(self) -> str:
        """The browser image's name, for the ledger. Never raises."""
        if self._factory is None:
            try:
                return str(self._module().BROWSER_IMAGE)
            except Exception:
                pass
        return BROWSER_IMAGE_DEFAULT

    def available(self) -> tuple[bool, str]:
        if self._factory is not None:
            return True, ""
        try:
            module = self._module()
        except SandboxUnavailable as exc:
            return False, exc.message
        except Exception as exc:
            return False, f"the browser sandbox cannot be imported ({type(exc).__name__})"
        try:
            present = bool(module.image_present())
        except Exception:
            present = False
        if not present:
            return False, (
                f"the browser image {module.BROWSER_IMAGE} is not available (not built, or Docker does not answer); in the "
                f"MiniLocker checkout run: {module.BUILD_HINT}"
            )
        return True, ""

    def _start(self, egress: Any) -> Any:
        factory = self._factory if self._factory is not None else self._module().BrowserSandbox
        return factory(egress=egress)

    # -- one run ----------------------------------------------------------------------------------------------------------

    def run(
        self,
        steps: Sequence[dict[str, Any]],
        *,
        hosts: tuple[str, ...],
        timeout_s: int,
        ledger_path: str | None,
        label: str,
    ) -> PlanOutcome:
        ok, why = self.available()
        if not ok:
            return PlanOutcome(False, error_type="BrowserUnavailable", message=why)
        started = self._clock()
        ledger = _open_ledger(ledger_path)  # before any container exists, so a ledger problem leaks nothing
        manager = None
        box: Any = None
        outcome: PlanOutcome | None = None
        since = time.time()
        try:
            try:
                manager = self._pool.acquire(hosts) if self._pool is not None and hosts else None
                box = self._start(manager)
            except SandboxUnavailable as exc:
                return PlanOutcome(False, error_type="BrowserUnavailable", message=exc.message)
            except Exception as exc:  # MiniLocker's BrowserUnavailable and Docker errors
                return PlanOutcome(
                    False,
                    error_type="BrowserUnavailable",
                    message=f"the browser could not start ({type(exc).__name__})",
                )
            _log_created(ledger, box, _safe(box.hardening), label)
            deadline = self._clock() + max(MIN_TIMEOUT_S, timeout_s)
            fields: dict[str, Any] | None = None
            rest: dict[str, Any] = {}
            try:
                result = browser_plan.run_steps(steps, _Clocked(box, deadline, self._clock), hosts=hosts)
            except browser_plan.PlanFailure as failure:
                rest = {"error_type": _FAILURES[failure.kind], "message": failure.message}
            except Exception as exc:  # the container went away under the run
                rest = {
                    "error_type": "SandboxCrashed",
                    "message": f"the browser stopped working ({type(exc).__name__})",
                }
            else:
                fields = result.fields
                rest = {
                    "steps_run": result.steps_run,
                    "pages": tuple(result.visited),
                    "screenshots": result.screenshots,
                }
            duration_ms = max(0, round((self._clock() - started) * 1000))
            attempts, blocked = _log_egress(ledger, manager, getattr(box, "ip", ""), since)
            outcome = PlanOutcome(
                fields is not None,
                fields=fields,
                egress_attempts=attempts,
                blocked=blocked,
                duration_ms=duration_ms,
                **rest,
            )
        finally:
            destroyed = True
            if box is not None:
                destroyed = _destroy(ledger, box)
            if self._pool is not None and manager is not None:
                self._pool.release()
        return outcome if destroyed else replace(outcome, cleanup_ok=False)

    # -- gate G6 ----------------------------------------------------------------------------------------------------------

    def audit(self, *, ledger_path: str | None, label: str) -> BrowserAudit:
        """Raises `SandboxUnavailable` when there is a browser image but the container cannot be started (try again later)."""
        ok, why = self.available()
        if not ok:
            return BrowserAudit(None, why)
        ledger = _open_ledger(ledger_path)
        try:
            box = self._start(None)
        except Exception as exc:
            raise SandboxUnavailable(
                f"the browser sandbox could not start ({type(exc).__name__})", hint="run `toolforge doctor`"
            ) from exc
        try:
            hardening = _safe(box.hardening)
            _log_created(ledger, box, hardening, label)
        finally:
            _destroy(ledger, box)
        if not hardening:
            return BrowserAudit(
                None, "Docker did not report the container's settings, so nothing was checked"
            )
        return BrowserAudit(hardening)


def browser_for(runner: Any, settings: Settings) -> BrowserAdapter | None:
    """The browser that goes with a sandbox runner: MiniLocker's, on the same egress pool, for `MiniLockerRunner`; none otherwise.

    A stand-in or fake sandbox gets no browser, so a plan is never run by accident from a test or a dry setup.
    """
    from toolforge.sandbox.minilocker_runner import MiniLockerRunner

    if isinstance(runner, MiniLockerRunner):
        return BrowserAdapter(settings, pool=runner.pool)
    return None


# --- Helpers ---------------------------------------------------------------------------------------------------------------


def _open_ledger(path: str | None) -> ToolforgeLedger | None:
    return (
        None if path is None else ToolforgeLedger(path, Path(path).stem or f"browser-{uuid.uuid4().hex[:10]}")
    )


def _safe(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = call()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _log_created(ledger: ToolforgeLedger | None, box: Any, hardening: dict[str, Any], label: str) -> None:
    if ledger is None:
        return
    created: dict[str, Any] = {
        "name": str(getattr(box, "name", "")),
        "ip": str(getattr(box, "ip", "")),
        "kind": "browser",
        "label": textsafety.sanitize(label, max_chars=200),
    }
    if hardening:
        created["hardening"] = hardening
    ledger.append("sandbox", "sandbox.created", created)


def _log_egress(
    ledger: ToolforgeLedger | None, manager: Any, ip: str, since: float
) -> tuple[int | None, int | None]:
    """`(attempts, blocked)` from the proxy's log for this container, each also written as an `egress.*` event."""
    if manager is None:
        return None, None
    try:
        events = list(manager.events_for(ip, since))
    except Exception as exc:
        if ledger is not None:
            ledger.append("forge", "forge.egress.unobserved", {"reason": type(exc).__name__})
        return None, None
    for event in events:
        if ledger is None:
            break
        payload = {
            ("proxy_ts" if key == "ts" else key): value for key, value in event.items() if key != "decision"
        }
        payload["source"] = "browser"
        ledger.append("egress", "egress." + str(event.get("decision", "unknown")), payload)
    decisions = [str(e.get("decision")) for e in events]
    return len(decisions), decisions.count("blocked")


def _destroy(ledger: ToolforgeLedger | None, box: Any) -> bool:
    """Destroy the container and say so. False when it would not go (`toolforge doctor --clean` removes it)."""
    try:
        box.destroy()
    except Exception as exc:
        if type(exc).__name__ != "NotFound":  # docker.errors.NotFound: already gone
            log.warning("a browser sandbox could not be removed (%s)", type(exc).__name__)
            return False
    if ledger is not None:
        ledger.append(
            "sandbox", "sandbox.destroyed", {"name": str(getattr(box, "name", "")), "reason": "run finished"}
        )
    return True


__all__ = ["BrowserAdapter", "BrowserAudit", "PlanOutcome", "PlanRunner", "browser_for"]
