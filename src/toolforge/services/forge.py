"""`Forge.submit`: from "an agent submitted a package" to a recorded verdict.

The steps, in order, and what each one refuses:

1. Read the submission (`ToolPackage.from_submission`; hostile input, nothing is run) and validate it (P02).
2. A package that is not valid is rejected on the spot and is **not registered**: the registry holds only valid manifests. The report is
   still the pipeline's, so G1 lists every problem at once.
3. `name@version` is looked up. The same content again returns the stored report (or, for a version that an earlier run could not
   finish because the sandbox was unavailable, resumes it). Different content under the same `name@version` is refused:
   versions are immutable, bump `version`.
4. Otherwise the version is registered as `submitted` and the pipeline runs it, with at most `max_concurrent_verifications` runs at
   the same time; further submissions wait their turn.

A failure of the sandbox itself (`SandboxUnavailable`, `SandboxError`) is raised to the caller and leaves the version `verifying`, so
submitting the same content again picks it up where it stopped.

Proven by `tests/test_forge.py` (fake gates, real registry) and `tests/test_verify_local.py` (the real harness through a stand-in).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from toolforge.config import Settings
from toolforge.errors import RegistryError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, lifecycle
from toolforge.sandbox.base import SandboxRunner
from toolforge.verify.pipeline import (
    Gate,
    Pipeline,
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
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_verifications)
        self._pipeline = pipeline or Pipeline(
            settings, runner, registry, ledger_factory=ledger_factory, gates=gates, fuzz_inputs=fuzz_inputs
        )

    def submit(self, payload: Any, submitted_by: str) -> VerificationReport:
        """Verify a submission `{"manifest": {...}, "files": {...}}` and return the report. See the module docstring."""
        package = ToolPackage.from_submission(payload)
        return self.submit_package(package, submitted_by)

    def submit_package(self, package: ToolPackage, submitted_by: str) -> VerificationReport:
        """The same for a package that was read from a folder (`toolforge submit <dir>`)."""
        by = clip(submitted_by, 80).strip() or "unknown"
        if package.validate(self._settings).ok:
            existing = self._registry.find_version(package.manifest.name, package.manifest.version)
            if existing is not None:
                return self._existing(existing, package, by)
        with self._slots:
            return self._pipeline.run(package, by)

    def _existing(self, record: Any, package: ToolPackage, by: str) -> VerificationReport:
        if record.code_sha != package.code_sha:
            raise RegistryError(
                f"{record.label} is already registered with different content; versions are immutable",
                code="version_exists",
                hint="bump `version` in the manifest and set parent_version to the version you are replacing",
            )
        if record.status in IN_PROGRESS:
            with self._slots:
                return self._pipeline.run(package, by, version=record)
        return report_from_registry(self._registry, record)


__all__ = ["Forge"]
