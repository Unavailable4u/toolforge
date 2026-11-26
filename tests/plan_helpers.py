"""Builders shared by the browser plan tests (P14b): a valid plan package, a scripted browser container and a scripted `PlanRunner`.

Nothing here starts a browser or opens a connection.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from toolforge.models.package import ToolPackage
from toolforge.sandbox.browser_adapter import BrowserAudit, PlanOutcome

HOST = "news.example.org"
FRONT = f"https://{HOST}/topic/weather"

PLAN: dict[str, Any] = {
    "steps": [
        {"step": "goto", "url": f"https://{HOST}/topic/{{{{topic}}}}"},
        {"step": "extract_text", "field": "headlines", "as": "lines", "after": "Top"},
    ]
}
PAGE: dict[str, Any] = {"url": FRONT, "title": "Weather", "text": "Top\nRain on Tuesday\nSun on Friday"}
OUTPUT: dict[str, Any] = {"fields": {"headlines": ["Rain on Tuesday", "Sun on Friday"]}}
MANIFEST: dict[str, Any] = {
    "name": "weather_news",
    "version": "1.0.0",
    "kind": "browser_plan",
    "description": "Return the headline lines shown on one topic page of a placeholder news host.",
    "when_to_use": "The task asks for the headlines of a topic.",
    "entrypoint": "plan.json",
    "input_schema": {
        "type": "object",
        "properties": {"topic": {"type": "string", "minLength": 1, "maxLength": 40}},
        "required": ["topic"],
        "additionalProperties": False,
    },
    "output_schema": {"type": "object"},
    "permissions": {"network": [HOST], "browser": True},
    "examples": [{"input": {"topic": "weather"}, "output": OUTPUT}],
}

# What Docker reports for a container hardened the way MiniLocker hardens one, with no network.
HARDENED: dict[str, Any] = {
    "user": "65534:65534",
    "read_only_rootfs": True,
    "cap_drop": ["ALL"],
    "cap_add": None,
    "privileged": False,
    "security_opt": ["no-new-privileges"],
    "network_mode": "none",
    "binds": None,
}


def plan_payload(
    *,
    plan: Any = None,
    pages: list[dict[str, Any]] | None = None,
    files: dict[str, str] | None = None,
    **manifest_changes: Any,
) -> dict[str, Any]:
    """A valid submission of a browser plan, which a test may change in any way."""
    manifest = copy.deepcopy(MANIFEST)
    manifest.update(manifest_changes)
    all_files = {
        "plan.json": json.dumps(PLAN if plan is None else plan),
        "fixtures/front.json": json.dumps({"pages": [PAGE] if pages is None else pages}),
    }
    all_files.update(files or {})
    return {"manifest": manifest, "files": all_files}


def plan_package(**changes: Any) -> ToolPackage:
    return ToolPackage.from_submission(plan_payload(**changes))


class ScriptedRunner:
    """A `PlanRunner`: says what it is told to say and remembers what it was asked."""

    image = "minilocker-browser:latest"

    def __init__(
        self,
        outcome: PlanOutcome | None = None,
        *,
        ready: bool = True,
        why: str = "the browser image is not built",
        hardening: dict[str, Any] | None = None,
    ) -> None:
        self.outcome = outcome or PlanOutcome(
            True,
            fields=copy.deepcopy(OUTPUT["fields"]),
            steps_run=2,
            pages=(f"{HOST}/topic/weather",),
            egress_attempts=1,
            blocked=0,
            duration_ms=7,
        )
        self.ready, self.why = ready, why
        self.hardening = HARDENED if hardening is None else hardening
        self.runs: list[dict[str, Any]] = []
        self.audits: list[dict[str, Any]] = []

    def available(self) -> tuple[bool, str]:
        return (True, "") if self.ready else (False, self.why)

    def run(
        self,
        steps: Sequence[dict[str, Any]],
        *,
        hosts: tuple[str, ...],
        timeout_s: int,
        ledger_path: str | None,
        label: str,
    ) -> PlanOutcome:
        self.runs.append(
            {
                "steps": list(steps),
                "hosts": hosts,
                "timeout_s": timeout_s,
                "ledger_path": ledger_path,
                "label": label,
            }
        )
        return self.outcome

    def audit(self, *, ledger_path: str | None, label: str) -> BrowserAudit:
        self.audits.append({"ledger_path": ledger_path, "label": label})
        if not self.ready:
            return BrowserAudit(None, self.why)
        return BrowserAudit(self.hardening)
