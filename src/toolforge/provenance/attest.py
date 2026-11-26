"""The attestation: one signed statement about one tool version (S01).

An attestation says *who* wrote down these facts and that they have not changed since: the hashes of the package, the head of the
verification ledger, the outcome of each gate as the ledger recorded it, and which versions of ToolForge, MiniLocker and the runtime image
were in use. It never says the tool is safe. A receiver's own pipeline decides that (`docs/PROVENANCE.md`).

Canonical JSON is sorted keys, no whitespace, UTF-8. The signature is Ed25519 over the canonical JSON of every field except `signature`.
The attestation carries the signer's public key (`signer_key`) so that a stranger's signature can still be checked for integrity; whether the
signer is somebody the receiver trusts is a separate question that only the receiver's own key list answers (`keys.trust_of`).

Security properties, each proved in `tests/test_provenance_attest.py`:

* Parsing is strict: every field is required, unknown fields are refused, every string has a fixed shape and length, the file must be
  byte for byte the canonical JSON of what was verified (no repeated keys, no `true` for 1, no re-spacing), and an error message
  names the field and the kind of mistake, never the value (a hostile bundle cannot put text into a message).
* A flipped signature byte, a changed field, a swapped signer fingerprint and a swapped signer key each fail with their own error code.
* Canonical JSON does not depend on key order, spacing or the machine.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from pydantic import ValidationError as PydanticValidationError

from toolforge.errors import ProvenanceError
from toolforge.models.package import NAME_PATTERN, semver_tuple
from toolforge.provenance.keys import fingerprint

SCHEMA = 1
MAX_ATTESTATION_BYTES = 64 * 1024
GATE_EVENTS = {"forge.gate.passed": "passed", "forge.gate.failed": "failed", "forge.gate.skipped": "skipped"}

_HEX64 = r"^[0-9a-f]{64}$"
_ASCII_WORD = r"^[!-~]{1,120}$"  # printable ASCII without spaces: model ids such as `vendor/model-1:free`

Hex64 = Annotated[str, StringConstraints(pattern=_HEX64)]
AsciiWord = Annotated[str, StringConstraints(pattern=_ASCII_WORD)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)


class GateStatement(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^G[1-9][0-9]?$")]
    status: Literal["passed", "failed", "skipped"]
    summary_sha: Hex64


class ModelsUsed(_Strict):
    challenger: AsciiWord | None
    builder: AsciiWord | None


class Attestation(_Strict):
    schema_version: Literal[1] = Field(alias="schema")
    name: Annotated[str, StringConstraints(pattern=f"^{NAME_PATTERN}$")]
    version: Annotated[str, StringConstraints(max_length=40)]
    code_sha: Hex64
    blob_sha: Hex64
    gates: list[GateStatement]
    ledger_head: Hex64
    toolforge_version: Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")]
    runtime_image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    minilocker_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")] | None
    models: ModelsUsed
    created_at: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"),
    ]
    signer: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    signer_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+/]{43}=$")]
    signature: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+/]{86}==$")]

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if semver_tuple(value) is None:
            raise ValueError("not MAJOR.MINOR.PATCH")
        return value

    @field_validator("gates")
    @classmethod
    def _gates(cls, value: list[GateStatement]) -> list[GateStatement]:
        if len(value) > 12 or len({gate.id for gate in value}) != len(value):
            raise ValueError("at most one statement per gate, and no more than 12")
        return value

    def as_signed_dict(self) -> dict[str, Any]:
        """Every field but `signature`, as plain JSON: exactly what the signature covers."""
        document = self.model_dump(mode="json", by_alias=True)
        del document["signature"]
        return document


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. The same value always gives the same bytes."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ProvenanceError("a value cannot be written as canonical JSON", code="not_canonical") from None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gate_statements(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the ledger says each gate concluded: the last `forge.gate.<status>` event per gate, with the SHA-256 of its payload.

    The attestation is checked against the ledger with this same function, so a statement about a gate that the ledger does not
    bear out is caught on import.
    """
    last: dict[str, dict[str, str]] = {}
    for event in events:
        status = GATE_EVENTS.get(event.get("type")) if isinstance(event, dict) else None
        payload = event.get("payload") if status is not None else None
        gate = payload.get("gate") if isinstance(payload, dict) else None
        if status is None or not isinstance(gate, str):
            continue
        last[gate] = {"id": gate, "status": status, "summary_sha": sha256_hex(canonical_json(payload))}
    return [last[gate] for gate in sorted(last, key=lambda g: (len(g), g))]


def sign(unsigned: dict[str, Any], key: Ed25519PrivateKey, raw_public: bytes) -> dict[str, Any]:
    """Add `signer`, `signer_key` and `signature` to an attestation body. The private key is used here and nowhere else."""
    body = {
        **unsigned,
        "signer": fingerprint(raw_public),
        "signer_key": base64.b64encode(raw_public).decode("ascii"),
    }
    body["signature"] = base64.b64encode(key.sign(canonical_json(body))).decode("ascii")
    return body


def parse(data: bytes) -> Attestation:
    """Read an attestation strictly. Raises `ProvenanceError` (`attestation_invalid`); the message names fields, never their values."""
    if len(data) > MAX_ATTESTATION_BYTES:
        raise ProvenanceError(
            f"attestation.json is larger than {MAX_ATTESTATION_BYTES // 1024} KB", code="attestation_invalid"
        )
    try:
        document = json.loads(data, parse_constant=_reject_constant)
        if not isinstance(document, dict):
            raise ValueError("not an object")
        attestation = Attestation.model_validate(document, strict=True)
    except PydanticValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'attestation'}: {error['type']}"
            for error in exc.errors()[:5]
        )
        raise ProvenanceError(
            f"attestation.json is not a valid attestation ({problems})",
            code="attestation_invalid",
            hint="the bundle was not made by this version of ToolForge, or it was edited",
        ) from None
    except (ValueError, RecursionError):
        raise ProvenanceError("attestation.json is not valid JSON", code="attestation_invalid") from None
    # The signature covers the parsed fields, so the file must say exactly that and nothing else: a repeated key, `true` for 1 or a
    # re-spaced file would verify here yet read differently in another JSON reader.
    if canonical_json({**attestation.as_signed_dict(), "signature": attestation.signature}) != data:
        raise ProvenanceError(
            "attestation.json is not in canonical form (sorted keys, no spaces, no repeated or reinterpreted values)",
            code="attestation_not_canonical",
            hint="the file was edited or written again after it was signed; do not import this bundle",
        )
    return attestation


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def check_signature(attestation: Attestation) -> str:
    """Verify the signature against the key the attestation carries. Returns the signer's fingerprint.

    Raises `ProvenanceError`: `signer_mismatch` when the fingerprint is not the one of the carried key (a swapped signer), and
    `signature_invalid` when the signature does not match the statement. A valid signature from a key nobody trusts is still valid: whether
    to trust the signer is the receiver's decision (`keys.trust_of`).
    """
    try:
        raw = base64.b64decode(attestation.signer_key, validate=True)
        signature = base64.b64decode(attestation.signature, validate=True)
    except (binascii.Error, ValueError):
        raise ProvenanceError(
            "the signer key or signature is not valid base64", code="signature_invalid"
        ) from None
    if len(raw) != 32 or len(signature) != 64:
        raise ProvenanceError("the signer key or signature has the wrong length", code="signature_invalid")
    if fingerprint(raw) != attestation.signer:
        raise ProvenanceError(
            f"the signer {attestation.signer} is not the fingerprint of the key in the attestation",
            code="signer_mismatch",
            hint="the signer field was changed after signing; do not import this bundle",
        )
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(
            signature, canonical_json(attestation.as_signed_dict())
        )
    except (InvalidSignature, ValueError):
        raise ProvenanceError(
            "the signature does not match the statement: the attestation was changed after it was signed, or it is not signed by the key it names",
            code="signature_invalid",
            hint="do not import this bundle",
        ) from None
    return attestation.signer


__all__ = [
    "GATE_EVENTS",
    "SCHEMA",
    "Attestation",
    "GateStatement",
    "ModelsUsed",
    "canonical_json",
    "check_signature",
    "gate_statements",
    "parse",
    "sha256_hex",
    "sign",
]
