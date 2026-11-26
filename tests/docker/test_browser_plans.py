"""Browser plans against real containers: MiniLocker's browser sandbox and egress proxy (P14b).

Needs a Docker daemon and the browser image (docs/browser-plans.md); the two run tests also need the network, because they open
`https://example.com`. Skipped, with the reason, when the daemon or the image is missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.sandbox.browser_adapter import BrowserAdapter, browser_for
from toolforge.sandbox.minilocker_runner import MiniLockerRunner
from toolforge.verify.g6_containment import check_hardening

pytestmark = [pytest.mark.docker, pytest.mark.usefixtures("minilocker")]

HOST = "example.com"
STEPS = [
    {"step": "goto", "url": f"https://{HOST}/"},
    {"step": "extract_text", "field": "body"},
]


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[BrowserAdapter]:
    settings = Settings(home=tmp_path / "home")
    sandbox = MiniLockerRunner(settings=settings)
    adapter = browser_for(sandbox, settings)
    assert adapter is not None
    ready, why = adapter.available()
    if not ready:
        pytest.skip(why)
    yield adapter
    sandbox.shutdown()


def test_the_real_browser_container_meets_the_sandbox_invariants(
    browser: BrowserAdapter, tmp_path: Path
) -> None:
    audit = browser.audit(ledger_path=str(tmp_path / "audit.jsonl"), label="test")
    assert audit.hardening is not None, audit.reason
    assert check_hardening(audit.hardening) == ([], [])


def test_a_plan_reads_a_real_page_through_the_proxy(browser: BrowserAdapter, tmp_path: Path) -> None:
    ledger = str(tmp_path / "run.jsonl")
    outcome = browser.run(STEPS, hosts=(HOST,), timeout_s=60, ledger_path=ledger, label="test")
    assert outcome.ok, f"{outcome.error_type}: {outcome.message}"
    # The page is someone else's and its wording changes ("Example Domain" was not in the text read on 2026-11-27),
    # so this asks for no more than what shows that the page's text came back.
    assert "domain" in outcome.fields["body"].lower(), outcome.fields["body"]
    assert outcome.cleanup_ok and outcome.egress_attempts, "the proxy saw the connection"
    types = [event["type"] for event in ToolforgeLedger(ledger, "read").events]
    assert "sandbox.created" in types and "egress.allowed" in types and types[-1] == "sandbox.destroyed"


def test_the_proxy_blocks_a_host_the_run_was_not_given(browser: BrowserAdapter, tmp_path: Path) -> None:
    """The validator never lets such a plan through; this goes past it to show that the network refuses on its own."""
    steps = [{"step": "goto", "url": "https://www.iana.org/"}, STEPS[1]]
    outcome = browser.run(
        steps, hosts=(HOST,), timeout_s=60, ledger_path=str(tmp_path / "no.jsonl"), label="test"
    )
    assert not outcome.ok and outcome.error_type == "PlanFailed", outcome
    assert outcome.blocked
