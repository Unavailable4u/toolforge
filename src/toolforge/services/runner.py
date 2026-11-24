"""`ToolRunner`: one tool call, host side. It drives the harness in a fresh sandbox and turns what comes back into a `CallResult`.

Security properties, and the tests that prove them (`tests/test_tool_runner.py` unless named):
* Tool code is never run or imported in this process. Only a sandbox runs it (`test_the_runner_never_executes_tool_code`).
* The caller's arguments are checked against the tool's `input_schema` in a child process with a time limit, because a schema
  written by a tool author can hold a regular expression that never finishes (`test_a_hostile_schema_cannot_hang_a_call`).
* The result envelope is untrusted data. Only the last one that carries this call's random nonce is read; it must have exactly the
  documented shape; its result is size-checked again here; the text in it is stripped and clipped (`test_envelope_*`).
* A timeout or a kill is decided by the host's own view of the sandbox, never by what the envelope says
  (`test_a_timed_out_run_is_a_timeout_whatever_it_printed`), and `duration_ms` is measured here (`test_duration_is_measured_on_the_host`).
* Every sandbox that is opened is closed on every path (`test_the_sandbox_is_closed_on_every_path`).
* A blob is unpacked on the host without extracting anything: only plain files with safe names, within a size budget, are
  re-packed and handed to the sandbox (`test_unusable_blobs_are_refused`).
* The ledger of a call is written before the sandbox exists and after it is gone, never while it is open, because the sandbox's own
  runner appends to the same hash chain (`test_ledger_events_surround_the_sandbox`). Arguments and results never enter it, only
  their hashes and sizes (`test_no_argument_value_reaches_the_ledger`).
* A call that touches a decoy credential is refused and nothing it produced is kept: no result, stdout or stderr, because a decoy value
  could be in them. The active version is quarantined by actor `forge` and the event goes to the operator ledger, but only the
  decoy labels, never a value (`test_a_decoy_touched_during_a_call_fails_it_and_quarantines_the_version`,
  `test_the_quarantine_is_written_to_the_operators_ledger`). `RUNTIME_DECOYS=false` turns this off.
* The manifest's permissions are the sandbox's network (P09): no hosts means no network at all; hosts are reachable only through the
  egress proxy, and only for a version that was approved for exactly those hosts and secrets (a recorded approval with this
  `code_sha`; `test_a_network_tool_without_a_recorded_approval_is_refused`). A secret is a brokered service: the sandbox gets
  MiniLocker's placeholder, never a token, and a secret with no broker service is refused
  (`test_a_secret_without_a_broker_service_is_refused`). A package run straight from a folder never gets a network or a secret
  (`test_an_unverified_run_gets_no_network_and_no_secret`).
* A call that ends in a failure of the tool (`Crash`, `Timeout`, `SandboxCrashed`, `NoResult`, `PolicyViolation`) is captured with its error class
  and a shape fingerprint (P12, `services/failures.py`), and a failure rate over 40 percent in the last calls quarantines the active version
  (`tests/test_failures.py`). Only measured outcomes count; a handled `ToolError` does not.
* After every call the audit is read for real secrets: one found inside the sandbox fails the call, discards its output and
  quarantines the version, as a decoy does (`test_a_real_secret_in_the_sandbox_fails_the_call_and_quarantines`). An audit that could
  not look is recorded as `not_measured`, never as clean.

What a caller learns is limited to `CallResult`. Infrastructure problems (no Docker, a sandbox that could not be filled, a corrupt
blob) are raised as exceptions for the operator instead; a server must not forward their text to an agent.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import posixpath
import secrets
import tarfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import ValidationError as PydanticValidationError

from toolforge import schemacheck, textsafety
from toolforge.config import Settings
from toolforge.errors import RegistryError, SandboxError, ToolNotRunnable
from toolforge.ledger import OPERATOR_LEDGER
from toolforge.models.package import ToolPackage, json_problem
from toolforge.models.results import CallError, CallResult
from toolforge.packaging import check_rel_path
from toolforge.registry import VersionRecord, lifecycle
from toolforge.sandbox import harness
from toolforge.sandbox.base import RunOutput, SandboxRunner, SandboxSpec, decoy_violations
from toolforge.services import failures

log = logging.getLogger(__name__)

GRACE_S = 3  # a call may run this long beyond limits.timeout_s before the sandbox is killed (interpreter start-up, slow disks)
MAX_FILES = 20
MAX_FILES_BYTES = 1024 * 1024  # all attached files together
MAX_ARGS_BYTES = 1024 * 1024
WORKSPACE_MB = 64
MAX_UNPACKED_BYTES = WORKSPACE_MB * 1024 * 1024 * 3 // 4  # a package may fill three quarters of the workspace
MAX_BLOB_ENTRIES = 5000
ENVELOPE_OVERHEAD = 128 * 1024  # room for the tails, messages and a trace in the envelope
ESCAPE_FACTOR = (
    6  # JSON written as ASCII can be six times the size of the text (a control character becomes \u0000)
)
TEXT_CAP = harness.TAIL_CHARS
MESSAGE_CAP = 1000

# Tests turn the isolation off for speed; the shipped behaviour is isolated (see `schemacheck`).
ARG_CHECK_ISOLATED = True
ARG_CHECK_TIMEOUT_S = schemacheck.DEFAULT_TIMEOUT_S

LedgerFactory = Callable[[str | None, str], "LedgerLike"]


class LedgerLike(Protocol):
    def append(self, actor: str, type: str, payload: dict | None = None) -> dict: ...


class RegistryLike(Protocol):
    """The part of `toolforge.registry.Registry` that a call needs, so a runner can be tested with a stand-in."""

    blobs: Any

    def get_version(self, version_id: int) -> VersionRecord | None: ...
    def find_version(self, name: str, version: str) -> VersionRecord | None: ...
    def active_version(self, name: str) -> VersionRecord | None: ...
    def get_tool(self, name: str) -> Any: ...
    def list_approvals(self, status: str | None = None) -> list[Any]: ...
    def transition(
        self, version_id: int, to: str, *, actor: str, reason: str | None = None
    ) -> dict[str, Any]: ...
    def record_call(
        self,
        version_id: int,
        ok: bool,
        duration_ms: int,
        error_class: str | None = None,
        session_id: str | None = None,
    ) -> None: ...


# --- The envelope ------------------------------------------------------------------------------------------------------


class _EnvelopeError(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    type: str
    message: str = ""
    trace_tail: str = ""


class Envelope(BaseModel):
    """What the harness prints. Parsed strictly: a wrong type or a missing field makes the whole envelope unusable."""

    model_config = ConfigDict(strict=True, extra="ignore")

    ok: bool
    result: Any = None
    error: _EnvelopeError | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    truncated: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Envelope:
        if self.ok != (self.error is None):
            raise ValueError("ok and error disagree")
        if self.error is not None and self.error.type not in harness.ENVELOPE_ERROR_TYPES:
            raise ValueError("the tool reported an error type that only the host may use")
        return self


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_envelope(stdout: bytes, nonce: str) -> Envelope | None:
    """The envelope in `stdout` that carries `nonce`, or None. The last start marker wins; everything else in stdout is ignored.

    The harness escapes `<` and `>` inside the JSON, so a marker cannot occur inside an honest envelope's payload.
    """
    start = harness.start_marker(nonce)
    at = stdout.rfind(start)
    if at < 0:
        return None
    begin = at + len(start)
    end = stdout.find(harness.end_marker(nonce), begin)
    if end < 0:
        return None
    try:
        return Envelope.model_validate(json.loads(stdout[begin:end], parse_constant=_reject_constant))
    except (
        ValueError,
        RecursionError,
        PydanticValidationError,
    ):  # bad JSON, bad UTF-8, a wrong shape, absurd nesting
        return None


# --- Unpacking a blob ---------------------------------------------------------------------------------------------------


def _unusable(reason: str) -> ToolNotRunnable:
    return ToolNotRunnable(
        f"the frozen package cannot be used: {reason}",
        code="blob_unusable",
        hint="submit the tool again so it is frozen again; do not run this version",
    )


def _member_name(name: str) -> str | None:
    clean = posixpath.normpath(name)
    parts = clean.split("/")
    if "\x00" in name or clean.startswith("/") or ".." in parts or clean == ".":
        return None
    return clean


def repack_blob(blob: bytes) -> tuple[bytes, list[str]]:
    """Read a stored package blob and return `(gzipped tar to extract into /workspace, package file names)`.

    Two shapes are accepted, plain or gzipped: the package archive (`ToolPackage.to_tar`, names relative to the package root, with
    `manifest.json` at the top) and a frozen workspace (`pkg/manifest.json`, with the package, and `pkg/.deps`, under `pkg/`).
    Nothing is extracted on the host. Only plain files with safe names are kept, within `MAX_UNPACKED_BYTES` and
    `MAX_BLOB_ENTRIES`; a link, device or unsafe name refuses the whole blob.
    """
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_BLOB_ENTRIES:
                    raise _unusable(f"it holds more than {MAX_BLOB_ENTRIES} entries")
                if member.isdir():
                    continue
                if not member.isreg():
                    raise _unusable("it holds something that is not a plain file (a link or a device)")
                name = _member_name(member.name)
                if name is None:
                    raise _unusable("it holds a file with an unsafe name")
                total += member.size
                if total > MAX_UNPACKED_BYTES:
                    raise _unusable(f"it unpacks to more than {MAX_UNPACKED_BYTES // (1024 * 1024)} MB")
                handle = archive.extractfile(member)
                files[name] = handle.read() if handle is not None else b""
    except (tarfile.TarError, OSError, EOFError):
        raise _unusable("it is not a readable archive") from None
    if "manifest.json" in files:
        placed = {f"pkg/{name}": data for name, data in files.items()}
    elif "pkg/manifest.json" in files:
        placed = {name: data for name, data in files.items() if name.startswith("pkg/")}
    else:
        raise _unusable("it holds no manifest.json")
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as out:
            for name in sorted(placed):
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(placed[name]), 0, 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                out.addfile(info, io.BytesIO(placed[name]))
    return buffer.getvalue(), sorted(name[len("pkg/") :] for name in placed)


# --- Small helpers --------------------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _sdk_source() -> bytes:
    return resources.files("toolforge").joinpath("sdk.py").read_bytes()


def sdk_source() -> bytes:
    """The text of `toolforge.sdk`, which every sandbox gets as `/workspace/.tf/toolforge_sdk.py` (also used by the gates)."""
    return _sdk_source()


def _plain(text: object, limit: int) -> str:
    """Untrusted text made safe to show: valid Unicode, no control or invisible characters, at most `limit` long."""
    raw = text if isinstance(text, str) else ""
    return textsafety.sanitize(raw.encode("utf-8", "replace").decode("utf-8"), max_chars=limit)


def _shown(value: object, limit: int = 60) -> str:
    """A caller-chosen string, safe to put in a message."""
    return repr(_plain(str(value), limit))


@dataclass
class _Target:
    """What one call runs: a registered version, or (for `run_unverified`) a package that was never registered."""

    label: str  # name@version
    manifest: dict[str, Any]
    code_sha: str
    version_id: int | None  # None: nothing is recorded in the registry
    load_blob: Callable[[], bytes]
    ledger_dir: str
    unverified: bool = False
    status: str | None = None  # the registry status when the call was resolved (None: not registered)


@dataclass
class _Outcome:
    ok: bool
    result: Any = None
    error: CallError | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    truncated: bool = False
    sandbox_timed_out: bool = False
    result_bytes: int | None = None


def _requested(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    perms = manifest.get("permissions") if isinstance(manifest.get("permissions"), Mapping) else {}
    return {
        "network": sorted({str(h) for h in perms.get("network") or []}),
        "secrets": sorted({str(n) for n in perms.get("secrets") or []}),
    }


def real_secret_audit(measured: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a sandbox's audit says about real secrets: `{"state": "found" | "clean" | "not_measured", "names": [...]}`.

    `names` are the variable names MiniLocker found a real value of inside the sandbox (never a value). `clean` needs an audit
    that looked inside the sandbox and knew at least one secret to look for; anything less is `not_measured`, which is not a pass.
    """
    section = (measured or {}).get("secrets") if isinstance(measured, Mapping) else None
    if not isinstance(section, Mapping):
        return {"state": "not_measured", "names": []}
    found = [n for n in section.get("found") or [] if isinstance(n, str)]
    if found:
        return {"state": "found", "names": sorted({_plain(n, 60) for n in found})[:8]}
    if section.get("inside") is True and section.get("checked"):
        return {"state": "clean", "names": []}
    return {"state": "not_measured", "names": []}


def _failure(kind: str, message: str, hint: str = "", **rest: Any) -> _Outcome:
    return _Outcome(ok=False, error=CallError(type=kind, message=message, hint=hint), **rest)


class ToolRunner:
    """Runs tool versions in fresh sandboxes.

    `registry` may be None when only `run_unverified` is used. `ledger_factory(path, task_id)` returns something with `append`;
    pass `toolforge.ledger.ToolforgeLedger` in production. It is called once before the sandbox opens and once after it is
    closed, so a file-backed ledger must resume an existing file (ToolforgeLedger does).
    """

    def __init__(
        self,
        runner: SandboxRunner,
        registry: RegistryLike | None,
        settings: Settings,
        ledger_factory: LedgerFactory,
        *,
        clock: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] | None = None,
        broker_services: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._broker_services = (
            broker_services  # `{service: host}` the operator configured; None asks MiniLocker when needed
        )
        self._runner = runner
        self._registry = registry
        self._settings = settings
        self._ledger_factory = ledger_factory
        self._clock = clock
        self._new_id = new_id or (lambda: uuid.uuid4().hex[:16])

    # -- entry points ---------------------------------------------------------------------------------------------------

    def run(
        self,
        name_or_version: str | VersionRecord,
        args: dict,
        *,
        files: Mapping[str, bytes] | None = None,
        session_id: str | None = None,
        allow_status: Sequence[str] = ("active",),
        record_stats: bool = True,
    ) -> CallResult:
        """Call a registered tool. `name_or_version` is a tool name (its active version), `name@version`, or a `VersionRecord`.

        Raises `RegistryError` for an unknown tool or version and `ToolNotRunnable` when the version's status is not in
        `allow_status`, it is a session tool, or its package cannot be unpacked. Bad arguments or files are not exceptions:
        they come back as a failed `CallResult` (`InvalidArguments`, `InvalidFiles`) after which nothing ran and nothing was
        recorded, because the tool is not at fault. `session_id` is recorded with the call; sessions are P13.
        `record_stats=False` keeps the call out of the version's statistics (the live smoke run of an approval uses it).
        """
        target = self._resolve(name_or_version, tuple(allow_status))
        return self._execute(target, args, files, session_id, record_stats)

    def run_unverified(
        self, package: ToolPackage, args: dict, *, files: Mapping[str, bytes] | None = None
    ) -> CallResult:
        """Call a package straight from a folder or submission, with no registry entry and no gate ever having run.

        For development only (`toolforge run --dev-unverified`). It still runs only in the sandbox, and it records nothing in
        the registry, so the statistics of a real tool are never touched. The ledger goes under `ledgers/_dev/`.
        """
        report = package.validate(self._settings)
        if not report.ok:
            first = report.issues[0]
            raise ToolNotRunnable(
                f"the package is not valid ({len(report.issues)} problems; first: [{first.code}] {first.message})",
                code="package_invalid",
                hint="run `toolforge validate <dir>` and fix what it reports",
            )
        manifest = package.manifest.model_dump(mode="json")
        blob = package.to_tar()
        label = f"{manifest['name']}@{manifest['version']}"
        target = _Target(
            label=label,
            manifest=manifest,
            code_sha=package.code_sha,
            version_id=None,
            load_blob=lambda: blob,
            ledger_dir=f"_dev/{label}",
            unverified=True,
        )
        self._require_stateless(target)
        wants = _requested(manifest)
        if wants["network"] or wants["secrets"]:
            raise ToolNotRunnable(
                f"{label} declares network hosts or secrets, and a package run straight from a folder never gets either",
                code="unverified_no_permissions",
                hint="submit it (`toolforge submit`) and have the operator approve it (`toolforge approvals`)",
            )
        return self._execute(target, args, files, None)

    # -- resolving ------------------------------------------------------------------------------------------------------

    def _resolve(self, ref: str | VersionRecord, allow_status: tuple[str, ...]) -> _Target:
        registry = self._registry
        if registry is None:
            raise ToolNotRunnable(
                "this runner has no registry", code="no_registry", hint="use run_unverified"
            )
        record: VersionRecord | None
        if isinstance(ref, VersionRecord):
            record = registry.get_version(
                ref.id
            )  # re-read: the status in the record the caller holds may be stale
        elif isinstance(ref, str) and "@" in ref:
            name, _, version = ref.strip().partition("@")
            record = registry.find_version(name, version)
        elif isinstance(ref, str):
            record = registry.active_version(ref.strip())
            if record is None and registry.get_tool(ref.strip()) is not None:
                raise ToolNotRunnable(
                    f"{_shown(ref)} has no active version",
                    code="no_active_version",
                    hint="list its versions with `toolforge registry show`; only an operator promotes a version",
                )
        else:
            raise TypeError("a tool is named by a string or a VersionRecord")
        if record is None:
            raise RegistryError(
                f"no tool or version matches {_shown(ref)}",
                code="unknown_version",
                hint="list what exists with `toolforge registry list`",
            )
        if record.status not in allow_status:
            raise ToolNotRunnable(
                f"{record.label} is {record.status}; calls are allowed only for: {', '.join(allow_status)}",
                code="status_not_allowed",
                hint="only an operator promotes a version to active (`toolforge promote`)",
            )
        sha = record.blob_sha
        if sha is None:
            raise ToolNotRunnable(
                f"{record.label} has no frozen package yet",
                code="no_blob",
                hint="the build gate freezes the package; run the verification pipeline",
            )
        target = _Target(
            label=record.label,
            manifest=record.manifest,
            code_sha=record.code_sha,
            version_id=record.id,
            load_blob=lambda: registry.blobs.get(sha),
            ledger_dir=record.label,
            status=record.status,
        )
        self._require_stateless(target)
        return target

    @staticmethod
    def _require_stateless(target: _Target) -> None:
        if target.manifest.get("kind", "stateless") != "stateless":
            raise ToolNotRunnable(
                f"{target.label} is a session tool and sessions are not built yet",
                code="session_not_built",
                hint="sessions arrive in a later release; only stateless tools can be called now",
            )

    # -- checking what the caller sent ----------------------------------------------------------------------------------------

    @staticmethod
    def _check_args(manifest: dict[str, Any], args: object) -> CallError | None:
        hint = "read the tool's input_schema and send exactly the properties it lists, with the right types"
        if not isinstance(args, dict):
            return CallError(
                type="InvalidArguments", message="the arguments must be a JSON object", hint=hint
            )
        problem = json_problem(args)
        if problem is not None:
            return CallError(
                type="InvalidArguments", message=f"the arguments are not plain JSON: {problem[1]}", hint=hint
            )
        if len(json.dumps(args).encode("utf-8")) > MAX_ARGS_BYTES:
            return CallError(
                type="InvalidArguments",
                message=f"the arguments are larger than {MAX_ARGS_BYTES // 1024} KB",
                hint="send less data inline; attach large content as a file if the tool takes files",
            )
        try:
            verdict = schemacheck.check_instances(
                [(manifest["input_schema"], [args])],
                isolated=ARG_CHECK_ISOLATED,
                timeout_s=ARG_CHECK_TIMEOUT_S,
            )[0][0]
        except (
            schemacheck.SchemaCheckError
        ) as exc:  # fail closed: an argument that cannot be checked is not run
            return CallError(
                type="InvalidArguments",
                message=f"the arguments could not be checked against the input schema ({exc})",
                hint="send simpler values",
            )
        if verdict is not None:
            return CallError(
                type="InvalidArguments",
                message=f"the arguments do not match the input schema: {verdict}",
                hint=hint,
            )
        return None

    @staticmethod
    def _check_files(manifest: dict[str, Any], files: object) -> CallError | None:
        def bad(message: str, hint: str = "") -> CallError:
            return CallError(type="InvalidFiles", message=message, hint=hint)

        if files is None or (isinstance(files, Mapping) and not files):
            return None
        if not (manifest.get("permissions") or {}).get("inputs"):
            return bad(
                "this tool takes no files (permissions.inputs is false)",
                "call it without files, or pick another tool",
            )
        if not isinstance(files, Mapping):
            return bad("files must map a path to the file's bytes")
        if len(files) > MAX_FILES:
            return bad(
                f"{len(files)} files were attached; at most {MAX_FILES} are allowed", "attach fewer files"
            )
        total = 0
        for path, data in files.items():
            why = check_rel_path(path)
            if why is not None:
                return bad(
                    f"file path {_shown(path)} {why}", "use a plain relative path such as inputs/data.csv"
                )
            if not path.startswith(harness.INPUTS_PREFIX) or path == harness.INPUTS_PREFIX:
                return bad(
                    f"file path {_shown(path)} does not start with inputs/", "name files inputs/<name>"
                )
            if not isinstance(data, (bytes, bytearray)):
                return bad(f"the content of {_shown(path)} must be bytes")
            total += len(data)
        if total > MAX_FILES_BYTES:
            return bad(
                f"the files are {total // 1024} KB together; the limit is {MAX_FILES_BYTES // 1024} KB",
                "attach less data",
            )
        names = set(files)
        for path in names:
            if any(other.startswith(path + "/") for other in names):
                return bad(
                    f"{_shown(path)} is both a file and a folder of other files",
                    "give every file its own name",
                )
        return None

    # -- the call ---------------------------------------------------------------------------------------------------------

    def _limits(self, manifest: dict[str, Any]) -> dict[str, int]:
        declared = manifest.get("limits") or {}
        return {
            "timeout_s": declared.get("timeout_s") or self._settings.default_timeout_s,
            "memory_mb": declared.get("memory_mb") or self._settings.default_memory_mb,
            "output_kb": declared.get("output_kb") or harness.DEFAULT_OUTPUT_KB,
        }

    def _ledger_path(self, target: _Target, call_id: str) -> str:
        return str(Path(self._settings.home) / "ledgers" / target.ledger_dir / f"{call_id}.jsonl")

    def _append(self, path: str, call_id: str, event: str, payload: dict[str, Any]) -> None:
        self._ledger_factory(path, call_id).append("forge", event, payload)

    def _brokers(self) -> Mapping[str, str]:
        if self._broker_services is not None:
            return self._broker_services()
        from toolforge.services import approvals

        known, _ = approvals.default_brokers(self._settings)
        return known

    def _authorize(self, target: _Target, hosts: tuple[str, ...], wanted_secrets: tuple[str, ...]) -> None:
        """Refuse a call that would give a tool a network or a secret nobody approved. Raises `ToolNotRunnable` (the operator's problem)."""
        if target.status not in (lifecycle.ACTIVE, lifecycle.PENDING_APPROVAL):
            raise ToolNotRunnable(
                f"{target.label} asks for network hosts or secrets and is {target.status}; it only runs once it is active",
                code="permissions_not_approved",
                hint="the operator approves it with `toolforge approvals approve`",
            )
        if (
            target.status == lifecycle.ACTIVE
        ):  # defence in depth: the lifecycle already guarantees it, the record proves it
            asked = {"network": sorted(hosts), "secrets": sorted(wanted_secrets)}
            registry = self._registry
            approved = [
                a
                for a in (registry.list_approvals("approved") if registry is not None else [])
                if a.version_id == target.version_id
                and (a.requested or {}).get("code_sha") == target.code_sha
                and (a.requested or {}).get("requested") == asked
            ]
            if not approved:
                raise ToolNotRunnable(
                    f"{target.label} asks for network hosts or secrets and has no recorded approval for this content",
                    code="permissions_not_approved",
                    hint="the operator should look at `toolforge approvals list` and `toolforge report`; do not run it",
                )
        if wanted_secrets:
            absent = [name for name in wanted_secrets if name not in self._brokers()]
            if absent:
                raise ToolNotRunnable(
                    f"{target.label} needs a brokered secret for {', '.join(absent)} and no broker service of that name is configured",
                    code="broker_not_configured",
                    hint="the operator sets MINILOCKER_BROKER=<name>=<host> and MINILOCKER_BROKER_TOKEN_<NAME> for the ToolForge process",
                )

    def _execute(
        self,
        target: _Target,
        args: dict,
        files: Mapping[str, bytes] | None,
        session_id: str | None,
        record_stats: bool = True,
    ) -> CallResult:
        call_id = self._new_id()
        manifest = target.manifest
        refusal = self._check_args(manifest, args) or self._check_files(manifest, files)
        if refusal is not None:  # the caller's mistake: nothing opens, nothing is recorded
            return CallResult(ok=False, error=refusal, version=target.label, call_id=call_id)
        attached = {path: bytes(data) for path, data in (files or {}).items()}
        wants = _requested(manifest)
        hosts, brokered = tuple(wants["network"]), tuple(wants["secrets"])
        if (hosts or brokered) and not target.unverified:
            self._authorize(target, hosts, brokered)
        elif target.unverified:
            hosts, brokered = (), ()
        limits = self._limits(manifest)
        packed, package_files = repack_blob(target.load_blob())
        entry = str(manifest["entrypoint"]).rpartition(":")[0]
        if entry not in package_files:
            raise _unusable(f"the entrypoint file {_shown(entry)} is not in it")

        args_json = json.dumps(args, sort_keys=True).encode("utf-8")
        request = json.dumps({"args": args, "entrypoint": manifest["entrypoint"], "limits": limits}).encode(
            "utf-8"
        )
        nonce = secrets.token_hex(16)
        ledger_path = self._ledger_path(target, call_id)
        session = _plain(session_id, 80) if session_id is not None else None
        spec = SandboxSpec(
            image=self._settings.runtime_image,
            network=hosts
            or "none",  # the manifest's hosts, reachable only through the egress proxy; none means no route out
            broker=brokered,
            workspace_mb=WORKSPACE_MB,
            memory_mb=limits["memory_mb"],
            decoys=self._settings.runtime_decoys,
            ledger_path=ledger_path,
            label=f"call:{target.label}",
        )
        # Before any sandbox exists: if this cannot be written, nothing runs (no call without a record).
        self._append(
            ledger_path,
            call_id,
            "forge.call.started",
            {
                "call_id": call_id,
                "tool": target.label,
                "code_sha": target.code_sha,
                "unverified": target.unverified,
                "session_id": session,
                "args_sha256": hashlib.sha256(args_json).hexdigest(),
                "args_bytes": len(args_json),
                "files": len(attached),
                "file_bytes": sum(len(data) for data in attached.values()),
                "limits": limits,
                "network": list(hosts) or "none",
                "secrets": list(brokered),
            },
        )
        finished: dict[str, Any] = {"call_id": call_id, "ok": False, "error_type": "Aborted"}
        try:
            outcome, duration_ms, summary_info = self._in_sandbox(
                spec, packed, request, attached, nonce, limits
            )
            violations = summary_info.pop("violations", [])  # never stored as given
            real_secrets = summary_info.pop("real_secrets", [])
            if not spec.decoys:
                violations = []
            if violations:  # the tripwire: nothing the tool produced is kept, and the version is pulled
                outcome = self._decoy_outcome(violations)
                summary_info["decoy_violations"] = len(violations)
                summary_info["quarantined"] = self._quarantine(target, call_id, violations)
            elif (
                real_secrets
            ):  # a real secret inside the sandbox is worse still: same response, different reason
                outcome = self._secret_outcome()
                summary_info["real_secrets_found"] = len(real_secrets)
                summary_info["quarantined"] = self._quarantine(
                    target,
                    call_id,
                    (),
                    why=f"a real secret was found inside the sandbox during call {call_id} ({', '.join(real_secrets)})",
                )
            finished.update(
                ok=outcome.ok,
                error_type=outcome.error.type if outcome.error else None,
                duration_ms=duration_ms,
                truncated=outcome.truncated,
                sandbox_timed_out=outcome.sandbox_timed_out,
                result_bytes=outcome.result_bytes,
                **summary_info,
            )
            if record_stats:
                self._record(target, outcome, duration_ms, session, call_id)
                health = self._after_failure(target, outcome, args, attached, call_id)
                if health:
                    finished.update(health)
            return CallResult(
                ok=outcome.ok,
                result=outcome.result,
                error=outcome.error,
                stdout_tail=outcome.stdout_tail,
                stderr_tail=outcome.stderr_tail,
                duration_ms=duration_ms,
                version=target.label,
                call_id=call_id,
                truncated=outcome.truncated,
                sandbox_timed_out=outcome.sandbox_timed_out,
            )
        except BaseException as exc:
            finished["error_type"] = type(
                exc
            ).__name__  # our own exception classes: no tool text, no host detail
            raise
        finally:
            try:
                self._append(ledger_path, call_id, "forge.call.finished", finished)
            except (
                Exception
            ):  # the call is over and its outcome is known; say so loudly rather than lose the result
                log.exception(
                    "call %s of %s finished but its ledger event could not be written", call_id, target.label
                )

    def _in_sandbox(
        self,
        spec: SandboxSpec,
        packed: bytes,
        request: bytes,
        attached: dict[str, bytes],
        nonce: str,
        limits: dict[str, int],
    ) -> tuple[_Outcome, int, dict[str, Any]]:
        handle = self._runner.open(
            spec
        )  # SandboxUnavailable propagates: the operator's problem, not the tool's
        output: RunOutput | None = None
        duration_ms = 0
        try:
            handle.thaw(packed)
            handle.put(harness.HARNESS_PATH, harness.HARNESS_SOURCE.encode("utf-8"))
            handle.put(harness.SDK_PATH, _sdk_source())
            handle.put(harness.REQUEST_PATH, request)
            for path, data in attached.items():
                handle.put(f"{harness.WORKSPACE}/{path}", data)
            started = self._clock()
            try:
                output = handle.run(
                    ["python", harness.HARNESS_PATH, nonce],
                    limits["timeout_s"] + GRACE_S,
                    limits["output_kb"] * 1024 * ESCAPE_FACTOR + ENVELOPE_OVERHEAD,
                )
            except SandboxError:  # the container went away under the call
                output = None
            duration_ms = max(0, round((self._clock() - started) * 1000))
        finally:
            summary_info = self._close(handle)
        return self._interpret(output, nonce, limits), duration_ms, summary_info

    @staticmethod
    def _close(handle: Any) -> dict[str, Any]:
        try:
            summary = handle.close()
        except (
            Exception
        ):  # the call's outcome stands; a container that would not go is for `toolforge doctor --clean`
            log.exception("a sandbox could not be closed cleanly")
            return {"cleanup_ok": False}
        audit = real_secret_audit(summary.measured)
        return {
            "cleanup_ok": True,
            "egress_attempts": len(summary.egress_attempts),
            "decoy_events": len(summary.decoy_events),
            "real_secret_audit": audit["state"],
            "real_secrets": audit[
                "names"
            ],  # taken out again by `_execute`: variable names only, never a value
            "violations": decoy_violations(
                summary.decoy_events
            ),  # taken out again by `_execute`; never stored as given
        }

    @staticmethod
    def _decoy_names(violations: Sequence[dict[str, Any]]) -> list[str]:
        """The decoy labels (fixed MiniLocker names such as 'AWS access key', never a value), short and clean."""
        names = {
            _plain((e.get("payload") or {}).get("label") or (e.get("payload") or {}).get("item"), 60)
            for e in violations
        }
        return sorted(n for n in names if n)[:5]

    def _decoy_outcome(self, violations: Sequence[dict[str, Any]]) -> _Outcome:
        """What a caller is told when the tool touched a decoy: no result, no output (a decoy value could be in it), no detail."""
        return _failure(
            "PolicyViolation",
            "the tool looked for credentials while it ran, so its answer was discarded",
            "this version has been quarantined and an operator has to review it before it can run again; "
            "use another tool or ask the operator",
        )

    @staticmethod
    def _secret_outcome() -> _Outcome:
        return _failure(
            "PolicyViolation",
            "a real credential was found inside the sandbox while the tool ran, so its answer was discarded",
            "this version has been quarantined and an operator has to review it before it can run again; "
            "use another tool or ask the operator",
        )

    def _quarantine(
        self, target: _Target, call_id: str, violations: Sequence[dict[str, Any]], *, why: str | None = None
    ) -> bool:
        """Pull an active version out of use because it touched a decoy. True when this call did it.

        Only a registered, active version can be quarantined. A second call that finds the work done, or a version whose status
        changed meanwhile, is not an error: the call is refused all the same and the operator's ledger already says why.
        """
        registry = self._registry
        if registry is None or target.version_id is None or target.unverified:
            return False
        names = self._decoy_names(violations)
        reason = (
            why or f"a decoy credential was touched during call {call_id} ({', '.join(names) or 'unnamed'})"
        )
        try:
            event = registry.transition(
                target.version_id, lifecycle.QUARANTINED, actor="forge", reason=reason
            )
        except Exception:
            log.warning("could not quarantine %s after a decoy was touched", target.label, exc_info=True)
            return False
        event_type = str(event.pop("type", "forge.quarantined"))
        event.pop("related", None)
        path = str(Path(self._settings.home) / "ledgers" / target.ledger_dir / OPERATOR_LEDGER)
        try:
            self._ledger_factory(path, f"quarantine:{target.label}").append(
                "forge", event_type, {**event, "call_id": call_id, "decoys": names}
            )
        except Exception:  # the registry already says quarantined; losing this line must not undo that
            log.exception("%s was quarantined but the operator ledger could not be written", target.label)
        return True

    def _record(
        self, target: _Target, outcome: _Outcome, duration_ms: int, session: str | None, call_id: str
    ) -> None:
        if target.version_id is None or self._registry is None:
            return
        # A ToolError is the tool doing its job (it refused a call the caller can fix), so it is not held against the tool; the
        # error class is still stored. A crash, a timeout, a bad result and a killed sandbox are.
        succeeded = outcome.ok or (outcome.error is not None and outcome.error.type == "ToolError")
        try:
            self._registry.record_call(
                target.version_id,
                succeeded,
                duration_ms,
                outcome.error.type if outcome.error else None,
                session,
            )
        except Exception:  # statistics must never turn a finished call into an exception
            log.exception("call %s of %s ran but its statistics could not be recorded", call_id, target.label)

    def _after_failure(
        self, target: _Target, outcome: _Outcome, args: dict, attached: Mapping[str, bytes], call_id: str
    ) -> dict[str, Any]:
        """Capture a failed call and, when the recent record says so, pull the active version. Never raises: the call is over.

        Returns what the call's ledger event should add (`{}` when nothing happened).
        """
        error = outcome.error
        if (
            error is None
            or error.type not in failures.CAPTURED
            or target.version_id is None
            or target.unverified
            or self._registry is None
        ):
            return {}
        info: dict[str, Any] = {}
        try:
            info["failure_captured"] = failures.capture(
                self._registry,
                self._settings,
                version_id=target.version_id,
                manifest=target.manifest,
                error_class=error.type,
                args=args,
                files=attached,
            )
            if error.type in failures.QUARANTINE_FAILURES and target.status == lifecycle.ACTIVE:
                verdict = failures.judge(self._registry, target.version_id)
                if verdict is not None and self._quarantine(target, call_id, (), why=verdict.reason(call_id)):
                    info["auto_quarantined"] = {"failures": verdict.failures, "calls": verdict.calls}
        except Exception:  # the failure record must never turn a finished call into an exception
            log.exception("the failure of call %s of %s could not be recorded", call_id, target.label)
        return info

    # -- reading the outcome ------------------------------------------------------------------------------------------------

    def _interpret(self, output: RunOutput | None, nonce: str, limits: dict[str, int]) -> _Outcome:
        timeout_s, memory_mb, output_kb = limits["timeout_s"], limits["memory_mb"], limits["output_kb"]
        if output is None:
            return _failure(
                "SandboxCrashed",
                "the sandbox stopped working while the tool was running",
                "try the call again; if it keeps happening the operator should run `toolforge doctor`",
            )
        stderr = _plain(output.stderr.decode("utf-8", "replace")[-TEXT_CAP:], TEXT_CAP)
        if output.timed_out:  # the host's own view wins over anything the tool printed before it was stopped
            return _failure(
                "Timeout",
                f"the tool did not finish within {timeout_s} seconds and was stopped",
                "make it faster or handle large inputs more cheaply; the manifest's limits.timeout_s may be raised up to 120",
                stderr_tail=stderr,
                truncated=output.truncated,
                sandbox_timed_out=True,
            )
        envelope = parse_envelope(output.stdout, nonce)
        if envelope is not None:
            return self._from_envelope(envelope, output, output_kb)
        if output.truncated:
            return _failure(
                "OutputTooLarge",
                "the tool wrote so much straight to stdout that its result was lost",
                "print less; return the data from run() instead of printing it",
                stderr_tail=stderr,
                truncated=True,
            )
        if output.exit_code < 0 or output.exit_code >= 128:
            guess = (
                " (most likely it used more than limits.memory_mb)" if output.exit_code in (137, -9) else ""
            )
            return _failure(
                "SandboxCrashed",
                f"the tool's process was killed from outside (exit code {output.exit_code}){guess}",
                f"use less memory (the limit is {memory_mb} MB) or raise limits.memory_mb up to 512",
                stderr_tail=stderr,
            )
        return _failure(
            "NoResult",
            f"the tool ended without returning a result (exit code {output.exit_code})",
            "return a value from run(); do not call os._exit or close stdout",
            stderr_tail=stderr,
        )

    @staticmethod
    def _from_envelope(envelope: Envelope, output: RunOutput, output_kb: int) -> _Outcome:
        common: dict[str, Any] = {
            "stdout_tail": _plain(envelope.stdout_tail, TEXT_CAP),
            "stderr_tail": _plain(envelope.stderr_tail, TEXT_CAP),
            "truncated": envelope.truncated or output.truncated,
        }
        if envelope.error is not None:
            message = _plain(envelope.error.message, MESSAGE_CAP)
            hint = {
                "ToolError": "the tool refused on purpose; change the arguments or files as its message says",
                "Crash": "the tool failed unexpectedly and this counts against it; try other arguments or report it",
                "NotJson": "the tool must return dicts, lists, text, numbers, booleans or null",
                "OutputTooLarge": "the tool must return a smaller result",
            }[envelope.error.type]
            return _Outcome(
                ok=False,
                error=CallError(
                    type=envelope.error.type,
                    message=message,
                    hint=hint,
                    trace_tail=_plain(envelope.error.trace_tail, TEXT_CAP),
                ),
                **common,
            )
        try:  # the harness checked this already; the envelope is untrusted, so check again
            size = len(
                json.dumps(envelope.result, allow_nan=False, ensure_ascii=False).encode("utf-8", "replace")
            )
        except (TypeError, ValueError, RecursionError):
            return _failure(
                "NoResult", "the result in the envelope is not usable JSON", "run the tool again", **common
            )
        if size > output_kb * 1024:
            return _failure(
                "OutputTooLarge",
                f"the result is {size // 1024} KB as JSON but limits.output_kb is {output_kb}",
                "the tool must return a smaller result",
                **common,
            )
        return _Outcome(ok=True, result=envelope.result, result_bytes=size, **common)


__all__ = [
    "ARG_CHECK_ISOLATED",
    "Envelope",
    "LedgerFactory",
    "LedgerLike",
    "RegistryLike",
    "ToolRunner",
    "real_secret_audit",
    "parse_envelope",
    "repack_blob",
    "sdk_source",
]
