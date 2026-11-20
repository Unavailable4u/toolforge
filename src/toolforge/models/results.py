"""Result types shared across the code base. P02 adds the validation report, P05 adds `CallResult`; later patches add more."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field


class Issue(BaseModel):
    """One finding about a package. `code` is stable (catalogued in `toolforge.models.package.ISSUE_CODES`).

    `message` says what is wrong and `hint` says what to change; both are written for an agent to act on. Any text from
    the package that is echoed in a message is clipped and escaped first.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    code: str
    message: str
    hint: str = ""


class ValidationReport(BaseModel):
    """The outcome of validating a package: errors in `issues` (any one rejects the package) and advisory `warnings`."""

    issues: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return not self.issues


class CallError(BaseModel):
    """Why a call failed, written for the agent that made it.

    `type` is one of `InvalidArguments`, `InvalidFiles` (the caller's mistake; nothing ran), `ToolError`, `Crash`, `NotJson`,
    `OutputTooLarge` (reported by the harness inside the sandbox), `Timeout`, `SandboxCrashed` or `NoResult` (decided by the host).
    Every text field is clipped and stripped of control characters before it is put here, so it is safe to show.
    """

    type: str
    message: str
    hint: str = ""
    trace_tail: str = ""


class CallResult(BaseModel):
    """What a caller learns about one tool call, and nothing more (no host path, no Docker detail).

    `result` is any JSON value the tool returned and is `None` on failure. `duration_ms` is measured on the host, never taken from
    the sandbox. `truncated` says that some output was cut; `sandbox_timed_out` says that the host stopped the sandbox.
    """

    ok: bool
    result: Any = None
    error: CallError | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int = 0
    version: str = ""
    call_id: str = ""
    truncated: bool = False
    sandbox_timed_out: bool = False
