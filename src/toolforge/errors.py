"""Error types. Every error an agent or operator may read carries a stable `code` and an optional `hint`.

Messages say what failed, why, and what to change (see docs/guide/01-conventions.md). Later patches add subclasses here.
"""

from __future__ import annotations


class ToolForgeError(Exception):
    """Base class for every expected failure."""

    code = "error"

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.hint = hint

    def __str__(self) -> str:
        return self.message if not self.hint else f"{self.message} ({self.hint})"


class ConfigError(ToolForgeError):
    code = "config_error"


class ValidationError(ToolForgeError):
    code = "validation_error"


class SandboxUnavailable(ToolForgeError):
    code = "sandbox_unavailable"


class SandboxError(ToolForgeError):
    """A sandbox that was opened failed to do what was asked (a write, a thaw, a call on a killed container)."""

    code = "sandbox_error"


class ToolNotRunnable(ToolForgeError):
    """A tool version cannot be called: wrong status, a session tool, no frozen package, or a package that cannot be unpacked.

    This is for the operator or the pipeline, not for a tool's author: a bad argument or a bad file is a failed `CallResult`
    instead, because the tool is not at fault.
    """

    code = "tool_not_runnable"


class LedgerError(ToolForgeError):
    code = "ledger_error"


class RegistryError(ToolForgeError):
    code = "registry_error"


class IllegalTransition(ToolForgeError):
    code = "illegal_transition"


class GateFailed(ToolForgeError):
    code = "gate_failed"
