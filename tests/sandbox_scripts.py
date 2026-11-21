"""Scripted answers a sandbox could give to the harness, for the gate tests: honest, odd and forged."""

from __future__ import annotations

import json
from typing import Any

from toolforge.sandbox import RunOutput


def envelope(nonce: str, result: Any = None, *, ok: bool = True, error: dict | None = None) -> bytes:
    body = {
        "ok": ok,
        "result": result,
        "error": error,
        "stdout_tail": "",
        "stderr_tail": "",
        "duration_ms": 1,
        "truncated": False,
    }
    text = json.dumps(body).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<<TF:{nonce}>>{text}<<TF-END:{nonce}>>\n".encode()


def suite_report(**changes: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "stub": False,
        "files": 1,
        "ran": 4,
        "passed": 4,
        "skipped": 0,
        "failed_count": 0,
        "error_count": 0,
        "not_run": 0,
        "failed": [],
        "errors": [],
    }
    report.update(changes)
    return report


def item(
    index: int, result: Any = None, *, error: tuple[str, str] | None = None, ms: int = 3
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "index": index,
        "ok": error is None,
        "error": None if error is None else {"type": error[0], "message": error[1], "trace_tail": ""},
        "result_bytes": len(json.dumps(result)),
        "stderr_tail": "",
        "duration_ms": ms,
    }
    if error is None and result is not None:
        entry["result"] = result
    return entry


def batch_report(items: list[dict[str, Any]], not_run: int = 0) -> dict[str, Any]:
    return {"items": items, "not_run": not_run}


def mode_of(argv: list[str]) -> str:
    flags = argv[3:]
    return (
        "stub"
        if "--stub" in flags
        else "tests"
        if "--tests" in flags
        else "batch"
        if "--batch" in flags
        else "call"
    )


def request_of(put: dict[str, bytes]) -> dict[str, Any]:
    return json.loads(put["/workspace/.tf/in.json"])


def respond(argv: list[str], put: dict[str, bytes], answers: dict[str, Any]) -> RunOutput:
    """Answer one harness call from `answers[mode]`: a dict (the result), a callable `(request) -> dict`, or a RunOutput."""
    answer = answers[mode_of(argv)]
    if callable(answer):
        answer = answer(request_of(put))
    if isinstance(answer, RunOutput):
        return answer
    return RunOutput(0, envelope(argv[2], answer), b"", False, False)
