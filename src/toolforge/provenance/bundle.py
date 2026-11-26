"""Portable bundles: hand one verified tool version to another operator, with provenance, without asking them to trust it (S01).

A `.tfbundle` is a deterministic, uncompressed tar of exactly six files:

```
bundle.json        schema, tool name, version, created_at, toolforge version
manifest.json      the manifest
package.tar.gz     the frozen package blob, byte for byte as the registry stores it (its SHA-256 is `blob_sha`)
ledger.jsonl       the verification ledger of the version (MiniLocker's hash chain)
report.json        the Blast Radius report
attestation.json   the signed statement (`attest`)
```

Import is the point of this module, and it is strict about what a bundle may do:

1. The tar is read member by member and nothing is extracted. Only the six names are accepted, each once, as plain files within a size cap;
   an unsafe path, an unknown member, a duplicate or an oversized member refuses the whole bundle.
2. `blob_sha` and `code_sha` are recomputed from the bytes in the bundle. The ledger is parsed line by line, its hash chain is verified with
   MiniLocker's own `verify_events` (through `toolforge.ledger`), `ledger_head` must be the hash of its last event, the ledger must be about
   this very version, and the attested gate outcomes must be what the ledger says.
3. The signature is checked. A valid signature from a key the operator did not choose to trust is allowed and is shown as "unsigned by anyone
   you trust"; it proves who wrote the statement and that it is unchanged, never that the tool is safe.
4. Import hands the package to `Forge.submit_package` as `created_by = "import:<fingerprint>"`: registered as `submitted`, then the full
   local pipeline runs. What the bundle says about gates is displayed as information and is never copied into `gate_results`, never shortcuts a
   gate and never changes a status. Network and secret permissions follow the normal approval flow. Nothing is promoted: `promote` is the
   operator's.
5. Name collisions and version immutability are `Forge`'s own rules, unchanged.

`verify_bundle` performs steps 1 to 3 only and changes nothing. Proven by `tests/test_provenance_bundle.py` and `tests/test_provenance_cli.py`
(each failure has its own error code and message; the tampered, swapped, flipped and path-traversal bundles are built there).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import posixpath
import re
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic import ValidationError as PydanticValidationError

from toolforge import __version__, textsafety
from toolforge import ledger as ledger_io
from toolforge.errors import ProvenanceError, ToolForgeError, ToolNotRunnable
from toolforge.models.package import NAME_PATTERN, ToolPackage
from toolforge.packaging import MANIFEST_NAME, build_tar, check_rel_path, text_problem
from toolforge.provenance import attest, keys
from toolforge.registry import lifecycle
from toolforge.registry.db import utc_now
from toolforge.sandbox import _ml_path
from toolforge.sandbox.image_info import image_id
from toolforge.services.runner import MAX_BLOB_ENTRIES, MAX_UNPACKED_BYTES, repack_blob

if TYPE_CHECKING:
    from toolforge.config import Settings
    from toolforge.registry import Registry
    from toolforge.registry.db import VersionRecord
    from toolforge.verify.pipeline import VerificationReport

SUFFIX = ".tfbundle"
BUNDLE_SCHEMA = 1
IMPORT_ACTOR_PREFIX = "import:"

MEMBER_LIMITS: dict[str, int] = {
    "attestation.json": attest.MAX_ATTESTATION_BYTES,
    "bundle.json": 16 * 1024,
    "ledger.jsonl": ledger_io.MAX_LEDGER_BYTES,
    "manifest.json": 1024 * 1024,
    "package.tar.gz": MAX_UNPACKED_BYTES,
    "report.json": 1024 * 1024,
}
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_LEDGER_EVENTS = 100_000
MAX_BUILD_LEDGERS = 200
EXPORTABLE = (
    lifecycle.VERIFIED_BASIC,
    lifecycle.VERIFIED,
    lifecycle.PENDING_APPROVAL,
    lifecycle.ACTIVE,
    lifecycle.DEPRECATED,
)
_SAFE_ID = r"^[!-~]{1,120}$"


class _Header(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = Field(alias="schema")
    name: Annotated[str, StringConstraints(pattern=f"^{NAME_PATTERN}$")]
    version: Annotated[str, StringConstraints(max_length=40)]
    created_at: Annotated[str, StringConstraints(max_length=40)]
    toolforge_version: Annotated[str, StringConstraints(max_length=40)]


# --- Reading a bundle (step 1) --------------------------------------------------------------------------------------------------


def _is_unsafe_name(name: str) -> bool:
    return "\x00" in name or "\\" in name or name.startswith("/") or ".." in name.split("/")


def read_members(data: bytes) -> dict[str, bytes]:
    """The six members of a bundle, read without extracting anything. Raises `ProvenanceError` (`bundle_*`) for anything else."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise ProvenanceError(
            f"the bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB", code="bundle_too_large"
        )
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for count, member in enumerate(archive, start=1):
                if count > len(MEMBER_LIMITS) + 1:
                    raise ProvenanceError(
                        f"the bundle holds more than the {len(MEMBER_LIMITS)} expected members",
                        code="bundle_member_unknown",
                    )
                name = member.name
                if _is_unsafe_name(name):
                    raise ProvenanceError(
                        f"the bundle holds a member with an unsafe path ({textsafety.sanitize(name, max_chars=40)!r}); "
                        "a bundle holds six plain files and nothing else",
                        code="bundle_member_unsafe",
                        hint="do not import this bundle",
                    )
                if name not in MEMBER_LIMITS:
                    raise ProvenanceError(
                        f"the bundle holds a member that is not part of the format ({textsafety.sanitize(name, max_chars=40)!r})",
                        code="bundle_member_unknown",
                        hint=f"a bundle holds exactly: {', '.join(sorted(MEMBER_LIMITS))}",
                    )
                if name in members:
                    raise ProvenanceError(f"the bundle holds {name} twice", code="bundle_member_duplicate")
                if not member.isreg():
                    raise ProvenanceError(
                        f"{name} is not a plain file (it is a link, a folder or a device)",
                        code="bundle_member_unsafe",
                    )
                if member.size > MEMBER_LIMITS[name]:
                    raise ProvenanceError(
                        f"{name} is larger than its limit of {MEMBER_LIMITS[name] // 1024} KB",
                        code="bundle_member_too_large",
                    )
                handle = archive.extractfile(member)
                body = handle.read(MEMBER_LIMITS[name] + 1) if handle is not None else b""
                if len(body) > MEMBER_LIMITS[name]:
                    raise ProvenanceError(f"{name} is larger than its limit", code="bundle_member_too_large")
                members[name] = body
    except ProvenanceError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError, UnicodeError):
        raise ProvenanceError(
            "the file is not a ToolForge bundle: it is not a readable, uncompressed tar archive",
            code="bundle_unreadable",
            hint="bundles come from `toolforge export`",
        ) from None
    missing = sorted(set(MEMBER_LIMITS) - set(members))
    if missing:
        raise ProvenanceError(
            f"the bundle has no {', '.join(missing)}",
            code="bundle_member_missing",
            hint="it is incomplete or was not made by `toolforge export`",
        )
    return members


def read_bundle_file(path: str | os.PathLike[str]) -> bytes:
    """The bytes of a bundle file, refusing a link, a folder or a file over the size cap before reading it."""
    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        raise ProvenanceError(f"{path} does not exist", code="bundle_missing") from None
    except OSError as exc:
        raise ProvenanceError(f"{path} cannot be read ({exc.strerror})", code="bundle_unreadable") from None
    if not target.is_file() or target.is_symlink():
        raise ProvenanceError(f"{path} is not a regular file", code="bundle_unreadable")
    if info.st_size > MAX_BUNDLE_BYTES:
        raise ProvenanceError(
            f"the bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB", code="bundle_too_large"
        )
    try:
        with open(target, "rb") as handle:
            return handle.read(MAX_BUNDLE_BYTES + 1)
    except OSError as exc:
        raise ProvenanceError(f"{path} cannot be read ({exc.strerror})", code="bundle_unreadable") from None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _json_object(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ProvenanceError(f"{what} is not valid JSON", code="bundle_invalid") from None
    if not isinstance(value, dict):
        raise ProvenanceError(f"{what} is not a JSON object", code="bundle_invalid")
    return value


# --- The ledger (step 2) --------------------------------------------------------------------------------------------------------


def parse_ledger(data: bytes) -> list[dict[str, Any]]:
    """Every line of `ledger.jsonl` as an event. Unlike a ledger being written, a bundle has no excuse for a torn line: any bad line refuses it."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ProvenanceError("ledger.jsonl is not UTF-8 text", code="ledger_invalid") from None
    events: list[dict[str, Any]] = []
    for number, line in enumerate((ln for ln in text.split("\n") if ln.strip()), start=1):
        if len(events) >= MAX_LEDGER_EVENTS:
            raise ProvenanceError(
                f"ledger.jsonl holds more than {MAX_LEDGER_EVENTS} events", code="ledger_invalid"
            )
        try:
            event = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise ProvenanceError(
                f"ledger.jsonl line {number} is not valid JSON", code="ledger_invalid"
            ) from None
        if not isinstance(event, dict):
            raise ProvenanceError(f"ledger.jsonl line {number} is not an object", code="ledger_invalid")
        events.append(event)
    return events


def ledger_text(events: list[dict[str, Any]]) -> bytes:
    """The ledger as MiniLocker writes it (sorted keys, compact, ASCII), one event per line."""
    return "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events
    ).encode("ascii")


def check_ledger(events: list[dict[str, Any]], label: str, code_sha: str) -> str:
    """Verify the chain and that the ledger is about `label` with this code. Returns the hash of the last event (the head)."""
    ok, message = ledger_io.verify_chain(events)
    if not ok:
        raise ProvenanceError(
            f"the verification ledger does not verify: {message}",
            code="ledger_chain_broken",
            hint="the ledger was edited, reordered or cut; do not import this bundle",
        )
    head = events[-1].get("hash")
    if not isinstance(head, str):
        raise ProvenanceError("the last ledger event has no hash", code="ledger_chain_broken")
    submitted = [
        event.get("payload")
        for event in events
        if event.get("type") == "forge.submitted" and isinstance(event.get("payload"), dict)
    ]
    if not any(p.get("tool") == label and p.get("code_sha") == code_sha for p in submitted):
        raise ProvenanceError(
            f"the verification ledger is not about {label} with this code (no forge.submitted event names both)",
            code="ledger_mismatch",
            hint="the bundle mixes a ledger from another tool or another version",
        )
    return head


# --- The package inside the blob (step 2) ---------------------------------------------------------------------------------------


def package_from_blob(manifest: dict[str, Any], blob: bytes) -> ToolPackage:
    """Rebuild the `ToolPackage` that a frozen blob holds, without extracting or running anything.

    The blob must be acceptable to the runner (`repack_blob`: plain files, safe names, size caps), its own manifest must equal `manifest`,
    and every file that is not vendored (`.deps`) must be plain UTF-8 text at a plain relative path. Raises `ProvenanceError`
    (`blob_unusable`, `manifest_mismatch`).
    """
    try:
        repack_blob(blob)
    except ToolNotRunnable as exc:
        raise ProvenanceError(
            f"the frozen package in the bundle cannot be used: {exc.message}", code="blob_unusable"
        ) from None
    raw: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_BLOB_ENTRIES:
                    break
                if member.isreg():
                    handle = archive.extractfile(member)
                    raw[posixpath.normpath(member.name)] = handle.read() if handle is not None else b""
    except (tarfile.TarError, OSError, EOFError):
        raise ProvenanceError(
            "the frozen package in the bundle is not a readable archive", code="blob_unusable"
        ) from None
    prefix = "" if MANIFEST_NAME in raw else "pkg/"
    own_manifest = raw.get(f"{prefix}{MANIFEST_NAME}")
    try:
        blob_manifest = json.loads((own_manifest or b"").decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        blob_manifest = None
    if blob_manifest != manifest:
        raise ProvenanceError(
            "the manifest inside the frozen package is not the manifest of the bundle",
            code="manifest_mismatch",
            hint="the bundle's parts come from different tools or versions; do not import it",
        )
    files: dict[str, str] = {}
    for name, body in raw.items():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix) :]
        if rel == MANIFEST_NAME or rel.startswith(".deps/"):
            continue
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or check_rel_path(rel) or text_problem(text):
            raise ProvenanceError(
                f"the frozen package holds a file that is not plain text at a plain path ({textsafety.sanitize(rel, max_chars=40)!r})",
                code="blob_unusable",
            )
        files[rel] = text
    return ToolPackage(manifest, files)


# --- Verifying (steps 1 to 3) ---------------------------------------------------------------------------------------------------


def _clean(value: str) -> str:
    return textsafety.sanitize(value, max_chars=120)


@dataclass(frozen=True)
class Signer:
    fingerprint: str
    trust: str  # keys.OWN, keys.TRUSTED or keys.UNTRUSTED
    label: str | None = None

    @property
    def trusted(self) -> bool:
        return self.trust in (keys.OWN, keys.TRUSTED)

    def describe(self) -> str:
        who = f"{self.fingerprint}" + (f" ({self.label})" if self.label else "")
        if self.trust == keys.OWN:
            return f"{who}: your own key"
        if self.trust == keys.TRUSTED:
            return f"{who}: a key you trust"
        return f"{who}: unsigned by anyone you trust"


@dataclass
class VerifiedBundle:
    """What steps 1 to 3 established. `package` is rebuilt from the bytes in the bundle, never from anything a person typed."""

    path: str
    bundle_sha256: str
    size: int
    attestation: attest.Attestation
    signer: Signer
    checks: list[str]
    ledger_events: int
    package: ToolPackage
    report_bytes: int

    @property
    def label(self) -> str:
        return f"{self.attestation.name}@{self.attestation.version}"

    def as_dict(self) -> dict[str, Any]:
        a = self.attestation
        return {
            "bundle": {"path": self.path, "sha256": self.bundle_sha256, "bytes": self.size},
            "tool": a.name,
            "version": a.version,
            "ok": True,
            "checks": list(self.checks),
            "signer": {
                "fingerprint": self.signer.fingerprint,
                "trust": self.signer.trust,
                "label": self.signer.label,
            },
            "signed_by_trusted_key": self.signer.trusted,
            "provenance": {
                "informational_only": True,
                "created_at": a.created_at,
                "toolforge_version": a.toolforge_version,
                "minilocker_commit": a.minilocker_commit,
                "runtime_image_digest": a.runtime_image_digest,
                "models": a.models.model_dump(),
                "gates_as_recorded_by_signer": [g.model_dump() for g in a.gates],
                "ledger_head": a.ledger_head,
                "ledger_events": self.ledger_events,
                "code_sha": a.code_sha,
                "blob_sha": a.blob_sha,
            },
        }

    def text(self) -> str:
        a = self.attestation
        gates = ", ".join(f"{g.id} {g.status}" for g in a.gates) or "none recorded"
        lines = [
            f"{self.label}: bundle {self.path} ({self.size // 1024} KB, sha256 {self.bundle_sha256[:16]}...)",
            *[f"  ok: {check}" for check in self.checks],
            f"signed by {self.signer.describe()}",
            "  A signature shows who made this bundle and that it is unchanged. It does not show the tool is safe: your own gates decide that.",
            "provenance, shown for information only (none of it is copied into your registry, skips a gate or changes a status):",
            f"  made {_clean(a.created_at)} with toolforge {_clean(a.toolforge_version)}; MiniLocker {a.minilocker_commit or 'not recorded'}",
            f"  runtime image {a.runtime_image_digest or 'not recorded'}",
            f"  models: challenger {_clean(a.models.challenger) if a.models.challenger else 'none'}, builder {_clean(a.models.builder) if a.models.builder else 'not recorded'}",
            f"  gates as the signer's ledger recorded them: {gates}",
            f"  ledger: {self.ledger_events} events, head {a.ledger_head[:16]}...",
        ]
        return "\n".join(lines)


def verify_members(
    members: dict[str, bytes], settings: Settings, *, path: str = "", bundle_sha256: str = "", size: int = 0
) -> VerifiedBundle:
    """Steps 2 and 3 on members already read. Raises `ProvenanceError` with the code of the first check that fails."""
    checks = ["the archive holds exactly the six expected files, each within its size limit"]
    attestation = attest.parse(members["attestation.json"])
    label = f"{attestation.name}@{attestation.version}"

    # The parts must all describe the same version.
    try:
        header = _Header.model_validate(_json_object(members["bundle.json"], "bundle.json"), strict=True)
    except PydanticValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()[:5])
        raise ProvenanceError(f"bundle.json is not valid ({problems})", code="bundle_invalid") from None
    manifest = _json_object(members["manifest.json"], "manifest.json")
    if (header.name, header.version) != (attestation.name, attestation.version) or (
        manifest.get("name"),
        manifest.get("version"),
    ) != (attestation.name, attestation.version):
        raise ProvenanceError(
            "bundle.json, manifest.json and attestation.json do not name the same tool and version",
            code="bundle_inconsistent",
            hint="the bundle's parts come from different tools or versions; do not import it",
        )

    # blob_sha, then code_sha (step 2): recomputed from the bytes that are in the bundle.
    blob = members["package.tar.gz"]
    if attest.sha256_hex(blob) != attestation.blob_sha:
        raise ProvenanceError(
            "the frozen package in the bundle does not match the blob_sha in the attestation",
            code="blob_sha_mismatch",
            hint="the package was changed after the bundle was made; do not import it",
        )
    checks.append("the frozen package matches blob_sha")
    package = package_from_blob(manifest, blob)
    try:
        code_sha = package.code_sha
    except ToolForgeError:
        raise ProvenanceError(
            "the package in the bundle cannot be rebuilt into an archive, so its code_sha cannot be checked",
            code="package_invalid",
        ) from None
    if code_sha != attestation.code_sha:
        raise ProvenanceError(
            "the package's code_sha, recomputed from its files, does not match the attestation",
            code="code_sha_mismatch",
            hint="the code does not match what was signed; do not import it",
        )
    checks.append("the package's code_sha, recomputed from its files, matches")

    events = parse_ledger(members["ledger.jsonl"])
    head = check_ledger(events, label, code_sha)
    checks.append(
        f"the verification ledger's hash chain verifies ({len(events)} events) and is about {label}"
    )
    if head != attestation.ledger_head:
        raise ProvenanceError(
            "the last event of the ledger is not the ledger_head in the attestation",
            code="ledger_head_mismatch",
            hint="events were added to or cut from the ledger after it was signed; do not import it",
        )
    checks.append("ledger_head is the hash of the ledger's last event")
    if [g.model_dump() for g in attestation.gates] != attest.gate_statements(events):
        raise ProvenanceError(
            "the gate outcomes in the attestation are not the ones the ledger records",
            code="gates_mismatch",
            hint="the attestation claims results its own ledger does not show; do not import it",
        )
    checks.append("the attested gate outcomes are the ones the ledger records")
    _json_object(members["report.json"], "report.json")

    # The signature (step 3).
    fingerprint = attest.check_signature(attestation)
    checks.append("the signature is valid for the key the attestation carries")
    known = keys.trust_of(settings.home, fingerprint)
    signer = Signer(fingerprint, known.kind if known else keys.UNTRUSTED, known.label if known else None)
    return VerifiedBundle(
        path=path,
        bundle_sha256=bundle_sha256,
        size=size,
        attestation=attestation,
        signer=signer,
        checks=checks,
        ledger_events=len(events),
        package=package,
        report_bytes=len(members["report.json"]),
    )


def verify_bundle(path: str | os.PathLike[str], settings: Settings) -> VerifiedBundle:
    """Steps 1 to 3 on a bundle file. Reads the file and the operator's key list; changes nothing."""
    data = read_bundle_file(path)
    return verify_members(
        read_members(data),
        settings,
        path=os.fspath(path),
        bundle_sha256=attest.sha256_hex(data),
        size=len(data),
    )


# --- Importing (step 4) ---------------------------------------------------------------------------------------------------------


@dataclass
class ImportResult:
    verified: VerifiedBundle
    report: VerificationReport
    already_registered: bool

    def text(self) -> str:
        note = (
            "\nnote: this version was already registered, so nothing was run; the stored verdict is shown"
            if self.already_registered
            else ""
        )
        return f"{self.verified.text()}\n\nlocal verification (the result that counts):\n{self.report.text()}{note}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle": self.verified.as_dict(),
            "already_registered": self.already_registered,
            "local_verification": self.report.as_dict(),
        }


def import_bundle(
    path: str | os.PathLike[str],
    settings: Settings,
    registry: Registry,
    submit: Callable[[ToolPackage, str], VerificationReport],
) -> ImportResult:
    """Verify a bundle (steps 1 to 3), then register it as `submitted` by `import:<fingerprint>` and run the whole local pipeline.

    `submit` is `Forge.submit_package`: it enforces the name collision and immutability rules, runs every gate again and opens the approval
    request of a tool that asks for network or secrets. Nothing the bundle says about gates reaches the registry, and the status that
    comes back is the local pipeline's.
    """
    verified = verify_bundle(path, settings)
    existing = registry.find_version(verified.attestation.name, verified.attestation.version)
    report = submit(verified.package, f"{IMPORT_ACTOR_PREFIX}{verified.signer.fingerprint}")
    return ImportResult(verified, report, existing is not None)


# --- Exporting ------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    path: str
    size: int
    sha256: str
    label: str
    signer: str
    ledger_head: str
    ledger_file: str

    def text(self) -> str:
        return (
            f"exported {self.label} to {self.path} ({self.size // 1024} KB, sha256 {self.sha256[:16]}...)\n"
            f"signed by {self.signer}; ledger head {self.ledger_head[:16]}... (from {self.ledger_file})\n"
            "check it with: toolforge verify-bundle " + self.path
        )


def _newest_first(paths: list[Path]) -> list[Path]:
    def age(path: Path) -> tuple[int, str]:
        try:
            return path.stat().st_mtime_ns, path.name
        except OSError:
            return 0, path.name

    return sorted(paths, key=age, reverse=True)


def select_verification_ledger(settings: Settings, label: str) -> tuple[Path, list[dict[str, Any]]]:
    """The newest `verify-*.jsonl` of the version that holds gate events (a run that only opened an approval request has none)."""
    folder = ledger_io.version_ledger_dir(settings.home, label)
    try:
        candidates = _newest_first([p for p in folder.glob("verify-*.jsonl") if p.is_file()])
    except OSError:
        candidates = []
    for path in candidates:
        events, problem = ledger_io.read_ledger(path)
        if problem is not None:
            raise ProvenanceError(
                f"the verification ledger {path.name} of {label} cannot be read cleanly ({problem})",
                code="ledger_invalid",
                hint="do not edit ledger files; the version cannot be exported while its ledger is damaged",
            )
        if any(event.get("type") in attest.GATE_EVENTS for event in events):
            return path, events
    raise ProvenanceError(
        f"{label} has no verification ledger on this machine, so there is no history to export",
        code="no_ledger",
        hint=f"looked in {folder}; a version that was verified here has one",
    )


def _builder_model(settings: Settings, record: VersionRecord) -> str | None:
    """The model that built this version, from the build ledgers (`forge.build.finished`). None when it is not recorded."""
    from toolforge.services import builder

    if record.created_by != builder.ACTOR:
        return None
    folder = ledger_io.version_ledger_dir(settings.home, builder.BUILD_LEDGER_DIR)
    try:
        files = _newest_first([p for p in folder.glob("*.jsonl") if p.is_file()])[:MAX_BUILD_LEDGERS]
    except OSError:
        return None
    for path in files:
        events, _ = ledger_io.read_ledger(path)
        for event in events:
            payload = event.get("payload")
            if event.get("type") != builder.EVENT or not isinstance(payload, dict):
                continue
            versions = payload.get("versions")
            if (
                payload.get("tool") == record.name
                and isinstance(versions, list)
                and record.version in versions
            ):
                model = payload.get("model")
                return model if isinstance(model, str) and _safe_id(model) else None
    return None


def _safe_id(value: str) -> bool:
    return re.fullmatch(_SAFE_ID, value) is not None


def _challenger_model(events: list[dict[str, Any]]) -> str | None:
    """The model G5 used, from the metrics of its ledger event. None when G5 was skipped or recorded no model."""
    found: str | None = None
    for event in events:
        payload = event.get("payload")
        if (
            event.get("type") in attest.GATE_EVENTS
            and isinstance(payload, dict)
            and payload.get("gate") == "G5"
        ):
            metrics = payload.get("metrics")
            model = metrics.get("model") if isinstance(metrics, dict) else None
            found = model if isinstance(model, str) and _safe_id(model) else None
    return found


def _report_bytes(settings: Settings, record: VersionRecord, ledger_path: Path) -> bytes:
    """The stored Blast Radius report, or one built from the chosen ledger. Building it stores nothing."""
    from toolforge.report import blast_radius

    stored = blast_radius.report_path(settings, record.label)
    with contextlib.suppress(OSError, ValueError):
        if stored.is_file() and stored.stat().st_size <= MEMBER_LIMITS["report.json"]:
            body = stored.read_bytes()
            if isinstance(json.loads(body.decode("utf-8")), dict):
                return body
    try:
        document = blast_radius.generate(
            settings, record.name, record.version, verification_path=ledger_path, store=False
        )
        body = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True).encode("utf-8")
    except Exception:
        raise ProvenanceError(
            f"the Blast Radius report of {record.label} could not be built",
            code="report_unavailable",
            hint=f"run `toolforge report {record.label}` to see why",
        ) from None
    if len(body) > MEMBER_LIMITS["report.json"]:
        raise ProvenanceError("the Blast Radius report is too large for a bundle", code="report_unavailable")
    return body


def export_bundle(
    settings: Settings,
    registry: Registry,
    ref: str,
    out: str | os.PathLike[str],
    *,
    force: bool = False,
    now: Callable[[], str] = utc_now,
    image_lookup: Callable[[str], str | None] | None = None,
) -> ExportResult:
    """Write `ref` (`name@version`) to the file `out` as a signed bundle. Reads the registry and the ledgers; changes neither.

    A version that was rejected, is still being verified or is quarantined is not exported. The bundle is verified by the same code an
    importer runs, before a byte is written, so an inconsistent registry (a blob that no longer matches its `code_sha`) is found here.
    """
    from toolforge.services import operator_actions

    name, version = operator_actions.parse_label(ref)
    record = registry.find_version(name, version)
    if record is None:
        raise ProvenanceError(
            f"no version {name}@{version}",
            code="unknown_version",
            hint="list what exists with `toolforge registry list`",
        )
    if record.status not in EXPORTABLE:
        raise ProvenanceError(
            f"{record.label} is {record.status}; only a version that passed verification can be exported",
            code="not_exportable",
            hint=f"exportable states: {', '.join(EXPORTABLE)}",
        )
    if record.blob_sha is None:
        raise ProvenanceError(
            f"{record.label} has no frozen package", code="no_blob", hint="it was never built"
        )
    out_path = Path(out)
    if out_path.exists() and not force:
        raise ProvenanceError(
            f"{out} already exists", code="output_exists", hint="choose another file or add --force"
        )

    private, raw_public = keys.load_private_key(settings.home)
    blob = registry.blobs.get(record.blob_sha)  # re-hashed: a damaged blob is refused here
    ledger_path, events = select_verification_ledger(settings, record.label)
    digest = (image_lookup or image_id)(settings.runtime_image)
    created = now()
    unsigned = {
        "schema": attest.SCHEMA,
        "name": record.name,
        "version": record.version,
        "code_sha": record.code_sha,
        "blob_sha": record.blob_sha,
        "gates": attest.gate_statements(events),
        "ledger_head": str(events[-1].get("hash")),
        "toolforge_version": __version__,
        "runtime_image_digest": digest
        if isinstance(digest, str) and digest.startswith("sha256:") and len(digest) == 71
        else None,
        "minilocker_commit": _ml_path.PINNED_COMMIT,
        "models": {"challenger": _challenger_model(events), "builder": _builder_model(settings, record)},
        "created_at": created,
    }
    header = {
        "schema": BUNDLE_SCHEMA,
        "name": record.name,
        "version": record.version,
        "created_at": created,
        "toolforge_version": __version__,
    }
    members = {
        "attestation.json": attest.canonical_json(attest.sign(unsigned, private, raw_public)),
        "bundle.json": attest.canonical_json(header),
        "ledger.jsonl": ledger_text(events),
        "manifest.json": attest.canonical_json(record.manifest),
        "package.tar.gz": blob,
        "report.json": _report_bytes(settings, record, ledger_path),
    }
    data = build_tar(members)
    for member, body in members.items():
        if len(body) > MEMBER_LIMITS[member]:
            raise ProvenanceError(
                f"{member} of {record.label} is larger than a bundle allows", code="bundle_member_too_large"
            )
    if len(data) > MAX_BUNDLE_BYTES:
        raise ProvenanceError("the bundle would be larger than the size cap", code="bundle_too_large")
    verified = verify_members(
        read_members(data),
        settings,
        path=os.fspath(out_path),
        bundle_sha256=attest.sha256_hex(data),
        size=len(data),
    )
    if verified.attestation.code_sha != record.code_sha:
        raise ProvenanceError(
            "the package in the registry does not match its code_sha", code="code_sha_mismatch"
        )
    _write_file(out_path, data, replace=force)
    return ExportResult(
        path=os.fspath(out_path),
        size=len(data),
        sha256=verified.bundle_sha256,
        label=record.label,
        signer=verified.signer.fingerprint,
        ledger_head=verified.attestation.ledger_head,
        ledger_file=ledger_path.name,
    )


def _write_file(path: Path, data: bytes, *, replace: bool) -> None:
    """Write through a temporary file in the same folder, so a reader never sees half a bundle."""
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=SUFFIX, dir=path.parent)
    except OSError as exc:
        raise ProvenanceError(
            f"cannot write to {path.parent} ({exc.strerror})", code="output_unwritable"
        ) from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        if not replace and path.exists():
            raise ProvenanceError(
                f"{path} already exists", code="output_exists", hint="choose another file or add --force"
            )
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


__all__ = [
    "EXPORTABLE",
    "MAX_BUNDLE_BYTES",
    "MEMBER_LIMITS",
    "SUFFIX",
    "ExportResult",
    "ImportResult",
    "Signer",
    "VerifiedBundle",
    "check_ledger",
    "export_bundle",
    "import_bundle",
    "ledger_text",
    "package_from_blob",
    "parse_ledger",
    "read_bundle_file",
    "read_members",
    "select_verification_ledger",
    "verify_bundle",
    "verify_members",
]
