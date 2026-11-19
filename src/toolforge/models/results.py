"""Result types shared across the code base. P02 adds the validation report; later patches add `CallResult` and friends."""

from __future__ import annotations

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
