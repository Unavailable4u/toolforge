"""The verification pipeline: gates run in order, each leaves a recorded result, and the first failure rejects the version.

Three things live here and nowhere else:

* `GATE_INFO`, the only list of gates in the repository (G1 to G7, in plain language, with `built` or `planned` derived from which
  gate classes are registered in `GATE_CLASS_PATHS`). Docs, the README and the drift tests read it; so will the site (P19).
* `Gate`, `GateContext` and `GateResult`, the interface every gate implements. A gate plugs in by adding one line to
  `GATE_CLASS_PATHS` (G5 did in P10); nothing else here changes.
* `Pipeline.run`, which moves a version `submitted -> verifying -> rejected | verified_basic | verified`, persists one `gate_results`
  row per gate, and writes the ledger events (`forge.submitted`, `forge.gate.*`, `forge.verifying`, `forge.verified`, `forge.rejected`).
  A version that passed and asks for network hosts or secrets then goes on to `pending_approval` (G7, `g7_approval`) and the report
  says so; the pipeline never waits for the operator.

Fail closed. A gate that raises is a failed gate. A gate that is not built yet is recorded as `skipped`, and a `skipped` gate caps the
status at `verified_basic`; only a run in which G1 to G6 all passed is `verified`. A failure of the sandbox itself (Docker is down) is
not the package's fault: it is raised to the caller, the version stays `verifying`, and submitting the same content again resumes it.

A gate reads the package and never changes it. Gates may not append to the ledger (the pipeline does, between gates), because a
sandbox that is open writes to the same chained file.

Proven by `tests/test_pipeline.py`: order, stop at the first failure, skipped capping, the events and rows written, a crashing gate,
an exceeded budget, a gate that tries to change the package, and that `GATE_INFO` lists G1 to G7 in order with the right status.
`tests/test_g7_approval.py` proves the hand-over to approval.
"""

from __future__ import annotations

import importlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from toolforge import textsafety
from toolforge.errors import SandboxError, SandboxUnavailable
from toolforge.registry import lifecycle
from toolforge.services import approvals

if TYPE_CHECKING:
    from toolforge.config import Settings
    from toolforge.models.package import ToolPackage
    from toolforge.registry import Registry
    from toolforge.registry.db import VersionRecord
    from toolforge.sandbox.base import SandboxRunner
    from toolforge.verify.g7_approval import ApprovalGate

log = logging.getLogger(__name__)

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
GATE_STATUSES = (PASSED, FAILED, SKIPPED)
NOT_RUN = "not_run"  # shown in a report for a gate after the one that failed; never stored
HINT_CHARS = 200  # a hint never carries more untrusted text than this
SUMMARY_CHARS = 240
MAX_HINTS = 25
ACTOR = "forge"


# --- The list of gates ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateInfo:
    id: str
    name: str
    plain_summary: str
    catches: str
    example: str
    status: str  # "built" or "planned"


_GATE_TEXT: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "G1",
        "Static check",
        "Reads the tool's files without running anything and looks for risky code, secret-shaped text and hidden instructions.",
        "Network libraries without declared hosts, native-code loaders, keys and tokens left in a file, and descriptions "
        "that try to give orders to an AI. It is a quick advisory scan that a determined author can get around; the sandbox "
        "is the real boundary.",
        "A tool that imports `socket` but declares no network hosts is rejected before anything runs.",
    ),
    (
        "G2",
        "Build",
        "Installs the tool's declared dependencies from ready-made wheels only and freezes everything into one sealed file.",
        "Dependencies that cannot be installed without running their setup scripts, versions that do not exist, "
        "undeclared dependencies, and packages too big for the sandbox.",
        "A pinned library that only exists as source code has no wheel, so the build fails instead of running its setup script.",
    ),
    (
        "G3",
        "Self-tests",
        "Runs the tool's own tests and its manifest examples in a sandbox with no network, then checks that the tests really check something.",
        "Tests that fail, examples whose answer is wrong, and tests that would still pass if the tool returned nothing.",
        "A suite made only of `assert True` still passes when the tool returns nothing, so it is rejected.",
    ),
    (
        "G4",
        "Schema fuzz",
        "Feeds the tool generated inputs that are valid by its own input schema: empty text, long text, zero, big numbers, empty lists.",
        "Crashes, hangs and oversized or non-JSON answers on inputs the schema says are allowed. Refusing with a clear "
        "message (`ToolError`) is fine; crashing is not.",
        "A tool that crashes on an empty list fails, with the input that broke it.",
    ),
    (
        "G5",
        "Challenger",
        "A second AI model, not the one that wrote the tool, tries to break it with inputs the author did not think of.",
        "Mistakes the author's own tests missed.",
        "The challenger finds that the tool miscounts words that contain an apostrophe.",
    ),
    (
        "G6",
        "Containment",
        "Runs the tool with the network closed and decoy secrets planted (fake keys, an SSH key, an .env file), and watches what it touches.",
        "Reading, printing or copying the decoy credentials, going past the memory, process or CPU limits, changing the container's own "
        "files, and a sandbox that is less locked down than it should be. The network is closed for this run, so it cannot show what a "
        "tool does with network access; a clean run is evidence, not proof.",
        "A tool that opens `/tmp/.aws/credentials` is caught by the decoy planted there.",
    ),
    (
        "G7",
        "Approval",
        "A human operator decides on tools that ask for network access or secrets.",
        "Permissions nobody agreed to.",
        "A tool that asks to call `api.example.org` waits until the operator approves exactly that host.",
    ),
)

# Gate id -> "module:Class". The one place a gate becomes built: a patch adds its line here (G5 in P10). G7 (P09) is built but
# is not in `PIPELINE_GATE_IDS`: it is the operator's approval, which `Pipeline.run` starts and does not wait for.
GATE_CLASS_PATHS: dict[str, str] = {
    "G1": "toolforge.verify.g1_static:StaticGate",
    "G2": "toolforge.verify.g2_build:BuildGate",
    "G3": "toolforge.verify.g3_selftest:SelfTestGate",
    "G4": "toolforge.verify.g4_fuzz:FuzzGate",
    "G5": "toolforge.verify.g5_challenger:ChallengerGate",
    "G6": "toolforge.verify.g6_containment:ContainmentGate",
    "G7": "toolforge.verify.g7_approval:ApprovalGate",
}

# The gates `Pipeline.run` goes through. G7 is the approval step `Pipeline.run` hands over to after these (see `g7_approval`).
PIPELINE_GATE_IDS: tuple[str, ...] = ("G1", "G2", "G3", "G4", "G5", "G6")


def _gate_info() -> tuple[GateInfo, ...]:
    return tuple(
        GateInfo(gid, name, summary, catches, example, "built" if gid in GATE_CLASS_PATHS else "planned")
        for gid, name, summary, catches, example in _GATE_TEXT
    )


GATE_INFO: tuple[GateInfo, ...] = _gate_info()


def gates_markdown() -> str:
    """The gate list as a Markdown table. `docs/verification.md` embeds exactly this text (drift-tested)."""
    rows = [
        "| Gate | Name | Status | What it does | What it catches | Example |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    rows += [
        f"| {g.id} | {g.name} | {g.status} | {g.plain_summary} | {g.catches} | {g.example} |"
        for g in GATE_INFO
    ]
    return "\n".join(rows)


def gate_name(gate_id: str) -> str:
    return next((g.name for g in GATE_INFO if g.id == gate_id), gate_id)


# --- Results and context -------------------------------------------------------------------------------------------------


def clip(text: object, limit: int = HINT_CHARS) -> str:
    """Untrusted text made safe to show: no control or invisible characters, at most `limit` characters."""
    raw = text if isinstance(text, str) else str(text)
    return textsafety.sanitize(raw.encode("utf-8", "replace").decode("utf-8"), max_chars=limit)


@dataclass
class GateResult:
    """What a gate reports. `summary` is one sentence, `hints` say what to change, `metrics` are numbers, `detail` is stored JSON."""

    status: str
    summary: str
    hints: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in GATE_STATUSES:
            raise ValueError(f"a gate status is one of {', '.join(GATE_STATUSES)}, not {self.status!r}")
        self.summary = clip(self.summary, SUMMARY_CHARS)
        self.hints = [clip(h) for h in self.hints][:MAX_HINTS]


def passed(summary: str, **parts: Any) -> GateResult:
    return GateResult(PASSED, summary, **parts)


def failed(summary: str, hints: Sequence[str] = (), **parts: Any) -> GateResult:
    return GateResult(FAILED, summary, hints=list(hints), **parts)


def skipped(summary: str, hints: Sequence[str] = (), **parts: Any) -> GateResult:
    return GateResult(SKIPPED, summary, hints=list(hints), **parts)


@dataclass
class GateContext:
    """Everything a gate may use. `blob` and `blob_sha` are set by G2 for the gates after it.

    `ledger_path` is only passed on to `SandboxSpec` so a sandbox's own events join the run's ledger: a gate never appends to it.
    `registry` and `version` are None in a dry run (`toolforge verify`); a gate that stores something (G2) must check.
    """

    package: ToolPackage
    settings: Settings
    runner: SandboxRunner | None = None
    registry: Registry | None = None
    version: VersionRecord | None = None
    ledger_path: str | None = None
    label: str = ""
    deadline: float = 0.0
    clock: Callable[[], float] = time.monotonic
    blob: bytes | None = None
    blob_sha: str | None = None
    original_sha: str = (
        ""  # the package's `code_sha` when the run began; the pipeline checks it again after every gate
    )

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - self.clock())


class Gate(Protocol):
    id: str
    budget_s: int

    def run(self, ctx: GateContext) -> GateResult: ...


def default_gates(**options: Any) -> list[Gate]:
    """An instance of every built gate, in order. `options` (such as `fuzz_inputs=30`) go to the gates that take them."""
    gates: list[Gate] = []
    for gate_id in PIPELINE_GATE_IDS:
        path = GATE_CLASS_PATHS.get(gate_id)
        if path is None:
            continue
        module_name, _, class_name = path.partition(":")
        cls = getattr(importlib.import_module(module_name), class_name)
        gates.append(cls(**{k: v for k, v in options.items() if k in getattr(cls, "OPTIONS", ())}))
    return gates


# --- The report ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateReport:
    id: str
    name: str
    status: str  # passed, failed, skipped or not_run
    summary: str
    hints: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "hints": list(self.hints),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class VerificationReport:
    """The outcome of one verification run.

    `status` is the version's lifecycle status (`rejected`, `verified_basic` or `verified`). `registered` is False for a dry run and
    for a package that was too invalid to register; then `version_id` is None and nothing was written to the registry.
    """

    status: str
    name: str
    version: str
    code_sha: str
    gates: tuple[GateReport, ...]
    warnings: tuple[str, ...]
    next_action: str
    registered: bool
    dry_run: bool
    version_id: int | None = None
    run_id: str = ""
    approval: dict[str, Any] | None = (
        None  # `{"id", "state"}` once G7 opened a request; None when the version needs none
    )

    @property
    def ok(self) -> bool:
        """Not rejected: verified, verified with a gate skipped, or waiting for the operator's approval (G7)."""
        return self.status in (lifecycle.VERIFIED_BASIC, lifecycle.VERIFIED, lifecycle.PENDING_APPROVAL)

    def as_dict(self) -> dict[str, Any]:
        document = self._as_dict()
        if self.approval is not None:
            document["approval"] = dict(self.approval)
        return document

    def _as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tool": self.name,
            "version": self.version,
            "code_sha": self.code_sha,
            "version_id": self.version_id,
            "registered": self.registered,
            "dry_run": self.dry_run,
            "run_id": self.run_id,
            "gates": [g.as_dict() for g in self.gates],
            "warnings": list(self.warnings),
            "next_action": self.next_action,
        }

    def text(self) -> str:
        """A plain-text rendering for the terminal."""
        label = f"{self.name}@{self.version}" if self.name else "(unreadable package)"
        lines = [f"{label}: {self.status}" + (" (dry run, nothing was written)" if self.dry_run else "")]
        for gate in self.gates:
            lines.append(f"  {gate.id} {gate.name}: {gate.status} - {gate.summary}")
            lines += [f"      hint: {hint}" for hint in gate.hints]
        lines += [f"  warning: {warning}" for warning in self.warnings]
        lines.append(f"next: {self.next_action}")
        return "\n".join(lines)


# --- Ledger --------------------------------------------------------------------------------------------------------------


class _RunLedger:
    """Appends to the run's chained ledger file, opening it fresh for every event.

    A sandbox that is open has its own writer on the same file and extends the chain from the last line when it opens; the pipeline
    only appends while no sandbox is open (a gate's sandbox is closed when the gate returns), so the chain stays unbroken. Opening the
    file again for each event resumes from whatever the sandbox wrote.
    """

    def __init__(self, path: str | None, task_id: str, factory: Callable[[str | None, str], Any]) -> None:
        self.path = path
        self._task_id = task_id
        self._factory = factory
        self.events: list[dict[str, Any]] = []

    def append(self, type: str, payload: dict[str, Any]) -> None:
        self.events.append({"type": type, **payload})
        if self.path is not None:
            self._factory(self.path, self._task_id).append(ACTOR, type, payload)


def permission_summary(package: ToolPackage) -> dict[str, Any]:
    try:
        permissions = package.manifest.permissions
    except Exception:
        return {}
    return {
        "network_hosts": len(permissions.network),
        "secrets": len(permissions.secrets),
        "inputs": permissions.inputs,
        "browser": permissions.browser,
    }


# --- The pipeline --------------------------------------------------------------------------------------------------------

ProgressFn = Callable[[str, str, str | None], None]


def _notify(progress: ProgressFn | None, gate_id: str, phase: str, status: str | None) -> None:
    """Tell an observer where the run is. An observer that breaks (a client that went away) must not stop a verification."""
    if progress is None:
        return
    try:
        progress(gate_id, phase, status)
    except Exception:
        log.debug("a progress observer failed on %s %s", gate_id, phase, exc_info=True)


def _next_action(
    status: str,
    failing: GateReport | None,
    registered: bool,
    skipped_ids: Sequence[str] = (),
    *,
    asks_permissions: bool = False,
    approval_id: int | None = None,
) -> str:
    if status == lifecycle.PENDING_APPROVAL:
        weaker = f" ({' and '.join(skipped_ids)} did not run, and the request says so)" if skipped_ids else ""
        where = (
            f"toolforge approvals show {approval_id}"
            if approval_id is not None
            else "toolforge approvals list"
        )
        return f"wait for the operator to decide on the network hosts or secrets it asks for{weaker}; they read `{where}`"
    if status in (lifecycle.VERIFIED, lifecycle.VERIFIED_BASIC) and asks_permissions:
        return (
            "it asks for network hosts or secrets, so a real submission goes to the operator for approval; "
            "promote does not apply to it"
        )
    if status == lifecycle.REJECTED and failing is not None:
        first = failing.hints[0] if failing.hints else failing.summary
        again = (
            "then submit it again with a higher version (a version is never changed once registered)"
            if registered
            else "then submit it again"
        )
        return clip(f"fix {failing.id} ({failing.name}): {first}; {again}", 400)
    if status == lifecycle.VERIFIED_BASIC:
        if not skipped_ids:
            return "wait for an operator to promote this version (it is not fully verified)"
        names = " and ".join(skipped_ids)
        verb = "has" if len(skipped_ids) == 1 else "have"
        return f"wait for an operator to promote this version ({names} {verb} not run, so it is not fully verified)"
    if status == lifecycle.VERIFIED:
        return "this version passed every gate and can be promoted"
    return "none"


class Pipeline:
    """Runs the gates. `registry=None` is a dry run: gates run, nothing is stored, no version is created."""

    def __init__(
        self,
        settings: Settings,
        runner: SandboxRunner | None,
        registry: Registry | None = None,
        *,
        ledger_factory: Callable[[str | None, str], Any] | None = None,
        gates: Sequence[Gate] | None = None,
        clock: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] | None = None,
        fuzz_inputs: int | None = None,
        approval: ApprovalGate | None = None,
        brokers: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._registry = registry
        self._ledger_factory = ledger_factory
        self._clock = clock
        self._new_id = new_id or (lambda: uuid.uuid4().hex[:12])
        options = {} if fuzz_inputs is None else {"fuzz_inputs": fuzz_inputs}
        self._gates = {gate.id: gate for gate in (gates if gates is not None else default_gates(**options))}
        self._approval = approval
        if self._approval is None and registry is not None:
            from toolforge.verify.g7_approval import ApprovalGate

            self._approval = ApprovalGate(settings, registry, brokers=brokers)

    # -- running -------------------------------------------------------------------------------------------------------

    def run(
        self,
        package: ToolPackage,
        submitted_by: str,
        *,
        version: VersionRecord | None = None,
        progress: ProgressFn | None = None,
    ) -> VerificationReport:
        """Verify `package`. With a registry the version is created (or `version`, left `submitted` or `verifying`, is resumed).

        `progress(gate_id, phase, status)` is called with phase `started` (status None) before each gate and `finished` (status
        `passed`, `failed` or `skipped`) after it. It runs on the calling thread and a failure in it never affects the run.
        """
        registry = self._registry
        record = version
        if registry is not None and record is None:
            # An invalid package cannot be registered (the registry stores only valid manifests); G1 still explains why.
            if package.validate(self._settings).ok:
                record = registry.add_version(package, created_by=submitted_by)
            else:
                registry = None
        run_id = self._new_id()
        ledger = self._open_ledger(package, record, run_id, registry)
        ctx = GateContext(
            package=package,
            settings=self._settings,
            runner=self._runner,
            registry=registry,
            version=record,
            ledger_path=ledger.path,
            label=record.label if record else self._label(package),
            clock=self._clock,
            blob=None,
            original_sha=self._code_sha(package, record),
        )
        if record is not None and registry is not None:
            self._start(registry, record, ledger, package, submitted_by, run_id)
        reports: list[GateReport] = []
        warnings: list[str] = []
        outcome = PASSED
        failing: GateReport | None = None
        for gate_id in PIPELINE_GATE_IDS:
            if failing is not None:
                reports.append(
                    GateReport(gate_id, gate_name(gate_id), NOT_RUN, "not run: an earlier gate failed")
                )
                continue
            _notify(progress, gate_id, "started", None)
            result = self._run_gate(gate_id, ctx, record, registry, ledger)
            _notify(progress, gate_id, "finished", result.status)
            warnings += [clip(w, 300) for w in result.detail.get("warnings", []) if isinstance(w, str)][:20]
            report = GateReport(
                gate_id,
                gate_name(gate_id),
                result.status,
                result.summary,
                tuple(result.hints),
                dict(result.metrics),
            )
            reports.append(report)
            if result.status == FAILED:
                outcome, failing = FAILED, report
            elif result.status == SKIPPED and outcome == PASSED:
                outcome = SKIPPED
        status = {PASSED: lifecycle.VERIFIED, SKIPPED: lifecycle.VERIFIED_BASIC, FAILED: lifecycle.REJECTED}[
            outcome
        ]
        if record is not None and registry is not None:
            reason = f"{failing.id}: {failing.summary}" if failing else None
            event = registry.transition(record.id, status, actor=ACTOR, reason=reason)
            ledger.append(event.pop("type"), event)
            if any(r.id == "G6" and r.status != NOT_RUN for r in reports) and ledger.path is not None:
                warnings += self._store_blast_radius(record, ledger.path)
        approval: dict[str, Any] | None = None
        next_action: str | None = None
        if (
            record is not None
            and registry is not None
            and status in (lifecycle.VERIFIED, lifecycle.VERIFIED_BASIC)
            and self._approval is not None
            and self._approval.needed(record)
        ):
            status, approval, next_action = self._hand_over(record, reports, ledger.append)
        return VerificationReport(
            status=status,
            name=record.name if record else self._name(package),
            version=record.version if record else self._version(package),
            code_sha=self._code_sha(package, record),
            gates=tuple(reports),
            warnings=tuple(warnings),
            next_action=next_action
            or _next_action(
                status,
                failing,
                record is not None,
                [r.id for r in reports if r.status == SKIPPED],
                asks_permissions=self._asks_permissions(package),
                approval_id=approval["id"] if approval else None,
            ),
            registered=record is not None,
            dry_run=self._registry is None,
            version_id=record.id if record else None,
            run_id=run_id,
            approval=approval,
        )

    @staticmethod
    def _asks_permissions(package: ToolPackage) -> bool:
        try:
            return approvals.asks_for_permissions(package.manifest.model_dump(mode="json"))
        except Exception:
            return False

    def _hand_over(
        self,
        record: VersionRecord,
        reports: Sequence[GateReport],
        append: Callable[[str, dict[str, Any]], None],
    ) -> tuple[str, dict[str, Any], str]:
        """G7: open the approval request of a verified version. Returns `(status, approval info, next action)`."""
        assert self._approval is not None
        requested = self._approval.request(record, [r.as_dict() for r in reports], append)
        info = {"id": requested.approval_id, "state": requested.state}
        if requested.state == "denied":
            return (
                lifecycle.REJECTED,
                info,
                clip(
                    f"the approval request ended at once: {requested.reason}; fix that, raise `version` and submit it again",
                    400,
                ),
            )
        skipped = [r.id for r in reports if r.status == SKIPPED]
        return (
            lifecycle.PENDING_APPROVAL,
            info,
            _next_action(lifecycle.PENDING_APPROVAL, None, True, skipped, approval_id=requested.approval_id),
        )

    def hand_over(self, record: VersionRecord) -> VerificationReport:
        """Open the approval request of a version that is verified and asks for permissions, and report on it.

        For a version an earlier run verified but could not hand over (the registry write failed after the last gate): submitting the
        same package again must not leave it stuck, because `promote` refuses it and nothing else moves it.
        """
        registry = self._registry
        if registry is None or self._approval is None:
            raise ValueError("hand_over needs a registry")
        current = registry.get_version(record.id)
        base = report_from_registry(registry, current if current is not None else record)
        if current is None or current.status not in (lifecycle.VERIFIED, lifecycle.VERIFIED_BASIC):
            return base
        ledger = self._open_ledger_for(current)
        status, approval, next_action = self._hand_over(current, base.gates, ledger.append)
        return VerificationReport(
            status=status,
            name=base.name,
            version=base.version,
            code_sha=base.code_sha,
            gates=base.gates,
            warnings=base.warnings,
            next_action=next_action,
            registered=True,
            dry_run=False,
            version_id=current.id,
            run_id=base.run_id,
            approval=approval,
        )

    def _open_ledger_for(self, record: VersionRecord) -> _RunLedger:
        run_id = self._new_id()
        factory = self._ledger_factory
        if factory is None:
            return _RunLedger(None, run_id, lambda *_: None)
        path = Path(self._settings.home) / "ledgers" / record.label / f"verify-{run_id}.jsonl"
        return _RunLedger(str(path), f"verify:{record.label}:{run_id}", factory)

    def _store_blast_radius(self, record: VersionRecord, ledger_path: str) -> list[str]:
        """Build and store the Blast Radius report of the run that just ended. A report that cannot be built is a warning, never a failure."""
        if not Path(ledger_path).is_file():
            return []  # an in-memory ledger (tests) wrote nothing to measure
        try:
            from toolforge.report import blast_radius

            blast_radius.generate(
                self._settings, record.name, record.version, verification_path=Path(ledger_path)
            )
        except Exception as exc:
            log.warning("could not store the Blast Radius report of %s", record.label, exc_info=True)
            return [
                f"the Blast Radius report could not be stored ({type(exc).__name__}); "
                f"`toolforge report {record.label}` builds it again"
            ]
        return []

    def _start(
        self,
        registry: Registry,
        record: VersionRecord,
        ledger: _RunLedger,
        package: ToolPackage,
        submitted_by: str,
        run_id: str,
    ) -> None:
        ledger.append(
            "forge.submitted",
            {
                "tool": record.label,
                "code_sha": record.code_sha,
                "submitted_by": clip(submitted_by, 80),
                "permissions": permission_summary(package),
                "run_id": run_id,
                "resumed": record.status == lifecycle.VERIFYING,
            },
        )
        if record.status == lifecycle.SUBMITTED:
            event = registry.transition(record.id, lifecycle.VERIFYING, actor=ACTOR)
            ledger.append(event.pop("type"), event)

    def _run_gate(
        self,
        gate_id: str,
        ctx: GateContext,
        record: VersionRecord | None,
        registry: Registry | None,
        ledger: _RunLedger,
    ) -> GateResult:
        gate = self._gates.get(gate_id)
        if gate is None:
            result = skipped(
                f"{gate_id} ({gate_name(gate_id)}) is not built in this release, so it did not run",
                ["the version stays at verified_basic until every gate has run"],
            )
            self._finish(gate_id, result, 0, record, registry, ledger, started_at=None)
            return result
        ledger.append("forge.gate.started", {"gate": gate_id, "tool": ctx.label})
        started_at = registry.db.now() if registry is not None else None
        began = self._clock()
        ctx.deadline = began + gate.budget_s
        try:
            result = gate.run(ctx)
            if not isinstance(result, GateResult):
                raise TypeError(f"a gate returns a GateResult, not {type(result).__name__}")
        except (SandboxUnavailable, SandboxError) as exc:
            ledger.append("forge.verification.aborted", {"gate": gate_id, "reason": exc.code})
            raise
        except Exception as exc:  # fail closed: a gate that breaks is a gate that failed
            log.exception("gate %s crashed on %s", gate_id, ctx.label)
            result = failed(
                f"{gate_id} stopped unexpectedly ({type(exc).__name__}), so the package is not accepted",
                [
                    "this is a ToolForge problem rather than a problem in the package; ask the operator to look at the log"
                ],
            )
        elapsed = self._clock() - began
        if self._code_sha(ctx.package, None) != ctx.original_sha:
            log.error("gate %s changed the package it was verifying (%s)", gate_id, ctx.label)
            result = failed(
                f"{gate_id} changed the package it was checking, so the result cannot be trusted",
                [
                    "this is a ToolForge problem rather than a problem in the package; ask the operator to look at the log"
                ],
            )
        if elapsed > gate.budget_s and result.status != FAILED:
            result = failed(
                f"{gate_id} took {elapsed:.0f} seconds; its limit is {gate.budget_s}",
                ["make the tool and its tests faster, or reduce what they do"],
                metrics=dict(result.metrics),
            )
        self._finish(gate_id, result, int(elapsed * 1000), record, registry, ledger, started_at)
        return result

    def _finish(
        self,
        gate_id: str,
        result: GateResult,
        duration_ms: int,
        record: VersionRecord | None,
        registry: Registry | None,
        ledger: _RunLedger,
        started_at: str | None,
    ) -> None:
        if record is not None and registry is not None:
            detail = {
                "summary": result.summary,
                "hints": result.hints,
                "metrics": result.metrics,
                **result.detail,
            }
            registry.record_gate(record.id, gate_id, result.status, detail, started_at=started_at)
        ledger.append(
            f"forge.gate.{result.status}",
            {
                "gate": gate_id,
                "tool": record.label if record else "",
                "summary": result.summary,
                "hints": len(result.hints),
                "metrics": result.metrics,
                "duration_ms": duration_ms,
            },
        )

    # -- small helpers -------------------------------------------------------------------------------------------------

    def _open_ledger(
        self, package: ToolPackage, record: VersionRecord | None, run_id: str, registry: Registry | None
    ) -> _RunLedger:
        factory = self._ledger_factory
        if registry is None or record is None or factory is None:
            return _RunLedger(None, run_id, lambda *_: None)
        path = Path(self._settings.home) / "ledgers" / record.label / f"verify-{run_id}.jsonl"
        return _RunLedger(str(path), f"verify:{record.label}:{run_id}", factory)

    @staticmethod
    def _name(package: ToolPackage) -> str:
        try:
            return package.manifest.name
        except Exception:
            return ""

    @staticmethod
    def _version(package: ToolPackage) -> str:
        try:
            return package.manifest.version
        except Exception:
            return ""

    def _label(self, package: ToolPackage) -> str:
        name = self._name(package)
        return f"{name}@{self._version(package)}" if name else "(unreadable package)"

    @staticmethod
    def _code_sha(package: ToolPackage, record: VersionRecord | None) -> str:
        if record is not None:
            return record.code_sha
        try:
            return package.code_sha
        except Exception:
            return ""


def report_from_registry(registry: Registry, record: VersionRecord) -> VerificationReport:
    """The report of a version that was verified earlier, built from its stored gate results (the latest row per gate)."""
    latest: dict[str, Any] = {}
    for row in registry.gate_results(record.id):
        latest[row.gate] = row
    gates: list[GateReport] = []
    warnings: list[str] = []
    failing: GateReport | None = None
    for gate_id in PIPELINE_GATE_IDS:
        row = latest.get(gate_id)
        if row is None:
            gates.append(GateReport(gate_id, gate_name(gate_id), NOT_RUN, "no result recorded"))
            continue
        detail = row.detail if isinstance(row.detail, dict) else {}
        report = GateReport(
            gate_id,
            gate_name(gate_id),
            row.status,
            clip(detail.get("summary", ""), SUMMARY_CHARS),
            tuple(clip(h) for h in detail.get("hints", []) if isinstance(h, str))[:MAX_HINTS],
            detail.get("metrics", {}) if isinstance(detail.get("metrics"), dict) else {},
        )
        gates.append(report)
        warnings += [clip(w, 300) for w in detail.get("warnings", []) if isinstance(w, str)][:20]
        if row.status == FAILED and failing is None:
            failing = report
    skipped = [g.id for g in gates if g.status == SKIPPED]
    latest = approvals.latest_approval(registry, record.id)
    info = {"id": latest.id, "state": latest.status} if latest is not None else None
    if record.status == lifecycle.PENDING_APPROVAL:
        next_action = _next_action(
            lifecycle.PENDING_APPROVAL, None, True, skipped, approval_id=latest.id if latest else None
        )
    elif (
        record.status == lifecycle.REJECTED
        and failing is None
        and latest is not None
        and latest.status == "denied"
    ):
        next_action = (
            "the permissions it asked for were not granted (the operator denied them, the request timed out, or a brokered "
            "service was not configured); change what it asks for, raise `version` and submit it again"
        )
    elif record.status in (lifecycle.REJECTED, lifecycle.VERIFIED_BASIC, lifecycle.VERIFIED):
        next_action = _next_action(
            record.status,
            failing,
            True,
            skipped,
            asks_permissions=approvals.asks_for_permissions(record.manifest),
        )
    else:
        next_action = f"this version is {record.status}"
    return VerificationReport(
        status=record.status,
        name=record.name,
        version=record.version,
        code_sha=record.code_sha,
        gates=tuple(gates),
        warnings=tuple(warnings),
        next_action=next_action,
        registered=True,
        dry_run=False,
        version_id=record.id,
        approval=info,
    )


__all__ = [
    "GATE_CLASS_PATHS",
    "GATE_INFO",
    "GATE_STATUSES",
    "PIPELINE_GATE_IDS",
    "FAILED",
    "PASSED",
    "SKIPPED",
    "Gate",
    "GateContext",
    "GateInfo",
    "GateReport",
    "GateResult",
    "Pipeline",
    "ProgressFn",
    "VerificationReport",
    "clip",
    "default_gates",
    "failed",
    "gate_name",
    "gates_markdown",
    "passed",
    "permission_summary",
    "report_from_registry",
    "skipped",
]
