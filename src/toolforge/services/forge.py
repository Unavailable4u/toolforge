"""`Forge.submit`: from "an agent submitted a package" to a recorded verdict.

The steps, in order, and what each one refuses:

1. Read the submission (`ToolPackage.from_submission`; hostile input, nothing is run) and validate it (P02).
2. A package that is not valid is rejected on the spot and is **not registered**: the registry holds only valid manifests. The report is
   still the pipeline's, so G1 lists every problem at once.
3. `name@version` is looked up. The same content again returns the stored report (or, for a version that an earlier run could not
   finish because the sandbox was unavailable, resumes it). Different content under the same `name@version` is refused:
   versions are immutable, bump `version`.
4. A new version of a tool that already has an active version must say it replaces that version (`parent_version` equal to the active
   version). Anything else is refused as a possible name collision: it may be a different tool that happens to share the name, and it
   must not be able to take the place of the one agents already use (P12 finishes this rule).
5. Otherwise the version is registered as `submitted` and the pipeline runs it, with at most `max_concurrent_verifications` runs at
   the same time. Further submissions wait their turn: forever for the command line (`queue_timeout_s=None`), or that many seconds
   and then `Busy` for a server that must answer its callers.

A failure of the sandbox itself (`SandboxUnavailable`, `SandboxError`) is raised to the caller and leaves the version `verifying`, so
submitting the same content again picks it up where it stopped.

Proven by `tests/test_forge.py` (fake gates, real registry), `tests/test_server_tools.py` (the same through MCP) and
`tests/test_verify_local.py` (the real harness through a stand-in).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from toolforge.config import Settings
from toolforge.errors import Busy, RegistryError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle
from toolforge.sandbox.base import SandboxRunner
from toolforge.verify.pipeline import (
    Gate,
    Pipeline,
    ProgressFn,
    VerificationReport,
    clip,
    report_from_registry,
)

IN_PROGRESS = (lifecycle.SUBMITTED, lifecycle.VERIFYING)


class Forge:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        runner: SandboxRunner | None,
        *,
        ledger_factory: Callable[[str | None, str], Any] | None = None,
        gates: list[Gate] | None = None,
        fuzz_inputs: int | None = None,
        pipeline: Pipeline | None = None,
        queue_timeout_s: float | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_verifications)
        self._queue_timeout_s = queue_timeout_s
        self._pipeline = pipeline or Pipeline(
            settings, runner, registry, ledger_factory=ledger_factory, gates=gates, fuzz_inputs=fuzz_inputs
        )

    def submit(
        self, payload: Any, submitted_by: str, *, progress: ProgressFn | None = None
    ) -> VerificationReport:
        """Verify a submission `{"manifest": {...}, "files": {...}}` and return the report. See the module docstring."""
        package = ToolPackage.from_submission(payload)
        return self.submit_package(package, submitted_by, progress=progress)

    def submit_package(
        self, package: ToolPackage, submitted_by: str, *, progress: ProgressFn | None = None
    ) -> VerificationReport:
        """The same for a package that was read from a folder (`toolforge submit <dir>`)."""
        by = clip(submitted_by, 80).strip() or "unknown"
        if package.validate(self._settings).ok:
            existing = self._registry.find_version(package.manifest.name, package.manifest.version)
            if existing is not None:
                return self._existing(existing, package, by, progress)
            self._check_lineage(package)
        with self._slot():
            return self._pipeline.run(package, by, progress=progress)

    @contextmanager
    def _slot(self) -> Iterator[None]:
        """One of the `max_concurrent_verifications` places. Waits for ever, or `queue_timeout_s` seconds and then raises `Busy`."""
        timeout = self._queue_timeout_s
        got = self._slots.acquire() if timeout is None else self._slots.acquire(timeout=timeout)
        if not got:
            raise Busy(
                f"{self._settings.max_concurrent_verifications} verifications are already running and none finished within "
                f"{timeout:g} seconds",
                hint="try again in a minute; submitting the same package again is safe",
            )
        try:
            yield
        finally:
            self._slots.release()

    def _check_lineage(self, package: ToolPackage) -> None:
        """A new version of a tool that is active must name that version as its parent, or it may be a different tool."""
        name, parent = package.manifest.name, package.manifest.parent_version
        active = self._registry.active_version(name)
        if active is not None and parent != active.version:
            raise RegistryError(
                f"a tool called {name} is already active at version {active.version}, and this package does not say it replaces "
                f"that version (its parent_version is {parent if parent else 'not set'})",
                code="name_collision",
                hint=(
                    f"to replace it, raise `version` and set parent_version to {active.version}; "
                    "if this is a different tool, choose another name"
                ),
            )

    def _existing(
        self, record: Any, package: ToolPackage, by: str, progress: ProgressFn | None
    ) -> VerificationReport:
        if record.code_sha != package.code_sha:
            raise RegistryError(
                f"{record.label} is already registered with different content; versions are immutable",
                code="version_exists",
                hint="bump `version` in the manifest and set parent_version to the version you are replacing",
            )
        if record.status in IN_PROGRESS:
            with self._slot():
                return self._pipeline.run(package, by, version=record, progress=progress)
        return report_from_registry(self._registry, record)


__all__ = ["Forge"]
