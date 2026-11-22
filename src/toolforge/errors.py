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


class Busy(ToolForgeError):
    """The server is working at its limit and a caller waited its turn in vain. Retrying later is the right response."""

    code = "busy"


class LLMError(ToolForgeError):
    """Base class for everything that goes wrong between ToolForge and a language-model provider."""

    code = "llm_error"


class LLMUnavailable(LLMError):
    """The model could not be used: not configured, unreachable, rate limited past the retry budget, or it refused our key.

    A gate that sees this records `skipped`, never `failed` and never `passed`: nothing was learned about the tool.
    """

    code = "llm_unavailable"


class LLMRefused(LLMError):
    """ToolForge refused to send a prompt because it contains a secret-shaped string. Nothing left the process."""

    code = "llm_refused"


class LLMBadReply(LLMError):
    """The provider answered, but not with one usable JSON object, even after one nudge. Treated like an outage by the gates."""

    code = "llm_bad_reply"
