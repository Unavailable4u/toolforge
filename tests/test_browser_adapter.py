"""`BrowserAdapter` (P14b) with a scripted browser sandbox and a scripted egress pool: no Docker, no browser, no network.

The real containers are covered by `tests/docker/test_browser_plans.py`.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from plan_helpers import HARDENED, HOST, OUTPUT, PAGE, PLAN

from toolforge.adapters import browser_plan
from toolforge.config import Settings
from toolforge.errors import SandboxUnavailable
from toolforge.ledger import ToolforgeLedger
from toolforge.sandbox import FakeRunner, browser_adapter
from toolforge.sandbox.browser_adapter import BrowserAdapter, browser_for

STEPS = browser_plan.render_plan(PLAN, {"topic": "weather"}, [HOST])
EVENTS = [
    {"decision": "allowed", "host": HOST, "port": 443, "ts": 1.0},
    {"decision": "blocked", "host": "ads.example.net", "port": 443, "ts": 2.0},
    {"decision": "closed", "host": HOST, "port": 443, "ts": 3.0},
]


class Box:
    """A scripted `BrowserSandbox`."""

    def __init__(self, egress: Any, rig: Rig) -> None:
        self.egress, self.rig = egress, rig
        self.name, self.ip = "minilocker-browser-abc", "10.9.0.5"
        self.destroyed = False
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def hardening(self) -> dict[str, Any]:
        if isinstance(self.rig.hardening, Exception):
            raise self.rig.hardening
        return self.rig.hardening

    def call(self, action: str, params: dict[str, Any], timeout_s: int = 40) -> dict[str, Any]:
        self.calls.append((action, params, timeout_s))
        self.rig.now += self.rig.seconds_per_call
        if self.rig.crash is not None:
            raise self.rig.crash
        return self.rig.reply

    def destroy(self) -> None:
        if self.rig.destroy_error is not None:
            raise self.rig.destroy_error
        self.destroyed = True


class Manager:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.asked: list[tuple[str, float]] = []

    def events_for(self, ip: str, since: float) -> list[dict[str, Any]]:
        self.asked.append((ip, since))
        if isinstance(self.rig.events, Exception):
            raise self.rig.events
        return self.rig.events


class Pool:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.acquired: list[tuple[str, ...]] = []
        self.released = 0
        self.busy = False

    def acquire(self, hosts: Any) -> Manager:
        if self.busy:
            raise SandboxUnavailable("egress is busy")
        self.acquired.append(tuple(hosts))
        return self.rig.manager

    def release(self) -> None:
        self.released += 1


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.now = 1000.0
        self.seconds_per_call = 0.0
        self.reply: dict[str, Any] = {"ok": True, "url": PAGE["url"], "text": PAGE["text"], "elements": []}
        self.hardening: Any = {**HARDENED, "network_mode": "minilocker-internal"}
        self.events: Any = list(EVENTS)
        self.crash: Exception | None = None
        self.start_error: Exception | None = None
        self.destroy_error: Exception | None = None
        self.boxes: list[Box] = []
        self.manager = Manager(self)
        self.pool = Pool(self)
        self.ledger = str(tmp_path / "call.jsonl")
        self.adapter = BrowserAdapter(pool=self.pool, browser_factory=self.factory, clock=lambda: self.now)

    def factory(self, egress: Any = None) -> Box:
        if self.start_error is not None:
            raise self.start_error
        self.boxes.append(Box(egress, self))
        return self.boxes[-1]

    def run(self, timeout_s: int = 30) -> browser_adapter.PlanOutcome:
        return self.adapter.run(
            STEPS, hosts=(HOST,), timeout_s=timeout_s, ledger_path=self.ledger, label="call:x@1.0.0"
        )

    def chain(self) -> list[dict[str, Any]]:
        return ToolforgeLedger(self.ledger, "read").events


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_one_run_takes_the_proxy_for_the_plans_hosts_and_gives_everything_back(rig: Rig) -> None:
    outcome = rig.run()
    assert outcome.ok and outcome.fields == OUTPUT["fields"] and outcome.cleanup_ok
    assert (outcome.steps_run, outcome.pages) == (2, (f"{HOST}/topic/weather",))
    assert (outcome.egress_attempts, outcome.blocked) == (3, 1)  # a closed connection is not a blocked one
    assert rig.pool.acquired == [(HOST,)] and rig.pool.released == 1
    box = rig.boxes[0]
    assert box.egress is rig.manager and box.destroyed
    assert rig.manager.asked[0][0] == box.ip


def test_the_ledger_holds_the_container_its_connections_and_its_end_but_no_page_text(rig: Rig) -> None:
    rig.run()
    events = rig.chain()
    assert [e["type"] for e in events] == [
        "sandbox.created",
        "egress.allowed",
        "egress.blocked",
        "egress.closed",
        "sandbox.destroyed",
    ]
    created = events[0]["payload"]
    assert created["kind"] == "browser" and created["hardening"]["user"] == "65534:65534"
    assert events[2]["payload"]["source"] == "browser" and events[2]["payload"]["host"] == "ads.example.net"
    assert events[2]["payload"]["proxy_ts"] == 2.0
    assert "Rain on Tuesday" not in Path(rig.ledger).read_text(encoding="utf-8")


def test_an_unreadable_proxy_log_is_not_measured_never_zero(rig: Rig) -> None:
    rig.events = RuntimeError("proxy gone")
    outcome = rig.run()
    assert outcome.ok and outcome.egress_attempts is None and outcome.blocked is None
    assert "forge.egress.unobserved" in [e["type"] for e in rig.chain()]


@pytest.mark.parametrize(
    ("reply", "error_type"),
    [
        ({"ok": False, "error": "TimeoutError: no element"}, "PlanFailed"),
        ({"ok": True, "url": PAGE["url"], "text": "no anchor here"}, "PlanFailed"),
        ({"ok": True, "url": "https://evil.example/", "text": "Top\nx"}, "HostLeft"),
    ],
)
def test_a_step_or_page_that_fails_is_reported_and_cleaned_up(rig: Rig, reply: dict, error_type: str) -> None:
    rig.reply = reply
    outcome = rig.run()
    assert not outcome.ok and outcome.fields is None and outcome.error_type == error_type and outcome.message
    assert rig.boxes[0].destroyed and rig.pool.released == 1
    assert outcome.egress_attempts == 3


def test_a_run_out_of_time_is_a_timeout(rig: Rig) -> None:
    rig.seconds_per_call = 100.0
    rig.reply = {"ok": False, "error": "the browser did not answer"}
    outcome = rig.run(timeout_s=30)
    assert outcome.error_type == "Timeout" and rig.boxes[0].destroyed


def test_no_call_is_made_after_the_deadline_and_each_gets_only_the_time_left(rig: Rig) -> None:
    rig.seconds_per_call = 20.0
    rig.reply = {"ok": True, "url": PAGE["url"]}  # no text, so the extract step has to call again
    outcome = rig.run(timeout_s=10)
    assert outcome.error_type == "Timeout" and len(rig.boxes[0].calls) == 1
    assert rig.boxes[0].calls[0][2] <= 11


def test_a_browser_that_disappears_is_a_crash(rig: Rig) -> None:
    rig.crash = RuntimeError("container died: /var/run/docker.sock")
    outcome = rig.run()
    assert outcome.error_type == "SandboxCrashed" and "docker.sock" not in outcome.message
    assert rig.pool.released == 1


def test_a_browser_that_cannot_start_releases_the_proxy_and_blames_nobody(rig: Rig) -> None:
    rig.start_error = RuntimeError("no such image")
    outcome = rig.run()
    assert outcome.error_type == "BrowserUnavailable" and outcome.steps_run == 0
    assert rig.pool.released == 1


def test_a_busy_proxy_is_unavailable_and_nothing_is_released(rig: Rig) -> None:
    rig.pool.busy = True
    outcome = rig.run()
    assert outcome.error_type == "BrowserUnavailable" and "busy" in outcome.message
    assert rig.boxes == [] and rig.pool.released == 0


def test_a_container_that_will_not_go_is_reported(rig: Rig) -> None:
    rig.destroy_error = RuntimeError("still there")
    outcome = rig.run()
    assert outcome.ok and outcome.cleanup_ok is False and rig.pool.released == 1
    assert "sandbox.destroyed" not in [e["type"] for e in rig.chain()]


def test_a_container_that_is_already_gone_counts_as_removed(rig: Rig) -> None:
    rig.destroy_error = type("NotFound", (Exception,), {})()
    assert rig.run().cleanup_ok is True


# --- The audit ---------------------------------------------------------------------------------------------------------------


def test_the_audit_starts_a_browser_with_no_network_and_returns_what_docker_says(rig: Rig) -> None:
    rig.hardening = HARDENED
    audit = rig.adapter.audit(ledger_path=rig.ledger, label="verify:x@1.0.0:G6")
    assert audit.hardening == HARDENED
    box = rig.boxes[0]
    assert box.egress is None and box.destroyed and box.calls == []  # no plan step runs
    assert rig.pool.acquired == []
    assert [e["type"] for e in rig.chain()] == ["sandbox.created", "sandbox.destroyed"]


@pytest.mark.parametrize("hardening", [{}, RuntimeError("inspect failed"), "text"])
def test_an_audit_that_learns_nothing_says_so(rig: Rig, hardening: Any) -> None:
    rig.hardening = hardening
    audit = rig.adapter.audit(ledger_path=None, label="x")
    assert audit.hardening is None and "nothing was checked" in audit.reason
    assert rig.boxes[0].destroyed


def test_an_audit_that_cannot_start_the_container_asks_to_try_again(rig: Rig) -> None:
    rig.start_error = RuntimeError("daemon busy")
    with pytest.raises(SandboxUnavailable):
        rig.adapter.audit(ledger_path=None, label="x")


# --- No browser at all --------------------------------------------------------------------------------------------------------


def test_without_minilocker_nothing_pretends_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_: Any, **__: Any) -> None:
        raise SandboxUnavailable("MiniLocker is not here")

    monkeypatch.setattr(browser_adapter._ml_path, "load", missing)
    adapter = BrowserAdapter()
    assert adapter.available() == (False, "MiniLocker is not here")
    assert adapter.image == "minilocker-browser:latest"
    outcome = adapter.run(STEPS, hosts=(HOST,), timeout_s=5, ledger_path=None, label="x")
    assert outcome.error_type == "BrowserUnavailable" and outcome.egress_attempts is None
    audit = adapter.audit(ledger_path=None, label="x")
    assert audit.hardening is None and audit.reason == "MiniLocker is not here"


def test_without_the_image_the_reason_says_how_to_build_it(monkeypatch: pytest.MonkeyPatch) -> None:
    class Module:
        BROWSER_IMAGE = "minilocker-browser:latest"
        BUILD_HINT = "docker build -f images/browser/Dockerfile -t minilocker-browser:latest ."

        @staticmethod
        def image_present() -> bool:
            return False

    monkeypatch.setattr(browser_adapter._ml_path, "load", lambda *a, **k: Module)
    ready, why = BrowserAdapter().available()
    assert not ready and Module.BUILD_HINT in why


def test_only_minilockers_own_sandbox_gets_a_browser(tmp_path: Path) -> None:
    from toolforge.sandbox.minilocker_runner import MiniLockerRunner

    settings = Settings(home=tmp_path)
    assert browser_for(FakeRunner(), settings) is None
    assert browser_for(None, settings) is None
    runner = MiniLockerRunner(settings=settings)
    adapter = browser_for(runner, settings)
    assert isinstance(adapter, BrowserAdapter) and adapter._pool is runner.pool


@pytest.mark.usefixtures("minilocker")
def test_the_pinned_minilocker_still_has_what_the_adapter_calls() -> None:
    pytest.importorskip("docker")
    from toolforge.sandbox import _ml_path

    module = _ml_path.load("minilocker.sandbox.browser")
    assert {"egress", "image"} <= set(inspect.signature(module.BrowserSandbox).parameters)
    for name in ("call", "hardening", "destroy"):
        assert callable(getattr(module.BrowserSandbox, name)), name
    assert module.BROWSER_IMAGE and module.BUILD_HINT and callable(module.image_present)
    worker = _ml_path.load("minilocker.sandbox.browser_worker")
    assert {"goto", "click", "type", "extract", "screenshot"} <= set(worker.ACTIONS)
    assert worker.MAX_WAIT_MS == browser_plan.MAX_WAIT_MS
