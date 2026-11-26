"""The attestation (`toolforge.provenance.attest`): canonical JSON, signing, strict parsing, and one error code for each way a signature fails."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from toolforge.errors import ProvenanceError
from toolforge.provenance import attest, keys

HOSTILE = "EVIL\x1b[31m-INJECTED-TEXT"


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    key = Ed25519PrivateKey.generate()
    return key, key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": 1,
        "name": "csv_sum",
        "version": "1.0.0",
        "code_sha": "a" * 64,
        "blob_sha": "b" * 64,
        "gates": [
            {"id": "G1", "status": "passed", "summary_sha": "1" * 64},
            {"id": "G5", "status": "skipped", "summary_sha": "5" * 64},
        ],
        "ledger_head": "d" * 64,
        "toolforge_version": "0.0.1",
        "runtime_image_digest": "sha256:" + "e" * 64,
        "minilocker_commit": "f" * 40,
        "models": {"challenger": "vendor/model-1:free", "builder": None},
        "created_at": "2026-11-26T12:00:00.000Z",
    }
    body.update(changes)
    return body


def _signed(key: Ed25519PrivateKey | None = None, raw: bytes | None = None, **changes: Any) -> dict[str, Any]:
    if key is None or raw is None:
        key, raw = _keypair()
    return attest.sign(_body(**changes), key, raw)


def _file(document: dict[str, Any]) -> bytes:
    return attest.canonical_json(document)


def _parse_fails(data: bytes, code: str = "attestation_invalid") -> ProvenanceError:
    with pytest.raises(ProvenanceError) as caught:
        attest.parse(data)
    assert caught.value.code == code
    return caught.value


def _code_of_check(document: dict[str, Any]) -> str:
    with pytest.raises(ProvenanceError) as caught:
        attest.check_signature(attest.parse(_file(document)))
    return caught.value.code


# --- Canonical JSON ----------------------------------------------------------------------------------------------------------


def test_canonical_json_ignores_key_order_and_is_compact() -> None:
    first = attest.canonical_json({"b": 1, "a": {"y": [1, 2], "x": None}})
    second = attest.canonical_json({"a": {"x": None, "y": [1, 2]}, "b": 1})
    assert first == second == b'{"a":{"x":null,"y":[1,2]},"b":1}'


def test_canonical_json_is_utf8_and_keeps_text_as_text() -> None:
    assert attest.canonical_json({"k": "é✓"}) == '{"k":"é✓"}'.encode()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"k": {1, 2}}, {"k": b"bytes"}, {1: object()}])
def test_a_value_that_is_not_json_is_refused(value: Any) -> None:
    with pytest.raises(ProvenanceError) as caught:
        attest.canonical_json(value)
    assert caught.value.code == "not_canonical"


def test_a_golden_value_pins_the_canonical_form() -> None:
    """Changing the canonical form invalidates every signature ever made, so the hash of one fixed statement is pinned here."""
    assert (
        attest.sha256_hex(attest.canonical_json(_body()))
        == "faf069bf35b20b096e7943de5fe49f563014fe7c2eaac05bacab9580a574b924"
    )


# --- Signing and checking ------------------------------------------------------------------------------------------------------


def test_a_signed_attestation_parses_and_verifies_and_names_its_signer() -> None:
    key, raw = _keypair()
    document = _signed(key, raw)
    parsed = attest.parse(_file(document))
    assert attest.check_signature(parsed) == keys.fingerprint(raw) == document["signer"]
    assert parsed.signer_key == base64.b64encode(raw).decode()
    assert parsed.name == "csv_sum" and parsed.models.builder is None


def test_signing_is_deterministic_for_the_same_key_and_statement() -> None:
    key, raw = _keypair()
    assert _signed(key, raw) == _signed(key, raw)


def test_the_signature_covers_every_field_but_itself() -> None:
    key, raw = _keypair()
    parsed = attest.parse(_file(_signed(key, raw)))
    assert set(parsed.as_signed_dict()) == set(_body()) | {"signer", "signer_key"}
    assert "signature" not in parsed.as_signed_dict()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "other_tool"},
        {"version": "1.0.1"},
        {"code_sha": "c" * 64},
        {"blob_sha": "c" * 64},
        {"ledger_head": "c" * 64},
        {"gates": []},
        {"toolforge_version": "9.9.9"},
        {"runtime_image_digest": None},
        {"minilocker_commit": None},
        {"models": {"challenger": None, "builder": None}},
        {"created_at": "2027-01-01T00:00:00Z"},
    ],
)
def test_changing_any_signed_field_breaks_the_signature(changes: dict[str, Any]) -> None:
    document = _signed()
    document.update(changes)
    assert _code_of_check(document) == "signature_invalid"


def test_a_flipped_signature_byte_fails_with_signature_invalid() -> None:
    document = _signed()
    raw = bytearray(base64.b64decode(document["signature"]))
    raw[10] ^= 0x01
    document["signature"] = base64.b64encode(bytes(raw)).decode()
    assert _code_of_check(document) == "signature_invalid"


def test_a_swapped_signer_fingerprint_fails_with_signer_mismatch() -> None:
    document = _signed()
    document["signer"] = keys.fingerprint(_keypair()[1])
    assert _code_of_check(document) == "signer_mismatch"


def test_a_swapped_signer_key_fails_even_when_the_fingerprint_is_made_to_match() -> None:
    """The attacker puts their own key and its fingerprint in, and keeps the honest signature."""
    document = _signed()
    _, attacker_raw = _keypair()
    document["signer_key"] = base64.b64encode(attacker_raw).decode()
    assert _code_of_check(document) == "signer_mismatch"  # honest fingerprint, attacker key
    document["signer"] = keys.fingerprint(attacker_raw)
    assert _code_of_check(document) == "signature_invalid"  # consistent, but the signature is not theirs


def test_a_valid_signature_from_a_stranger_is_valid() -> None:
    """Integrity is checked here; whether anyone trusts the signer is the receiver's key list, not this module."""
    parsed = attest.parse(_file(_signed()))
    assert attest.check_signature(parsed) == parsed.signer


def test_a_signature_made_for_another_statement_is_refused() -> None:
    key, raw = _keypair()
    first, second = _signed(key, raw), _signed(key, raw, version="2.0.0")
    first["signature"] = second["signature"]
    assert _code_of_check(first) == "signature_invalid"


# --- Strict parsing -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", list(_signed()))
def test_every_field_is_required(field: str) -> None:
    document = _signed()
    del document[field]
    _parse_fails(_file(document))


def test_unknown_fields_are_refused() -> None:
    document = _signed()
    document["trusted"] = True
    _parse_fails(_file(document))
    document = _signed()
    document["models"]["extra"] = "x"
    _parse_fails(_file(document))
    document = _signed()
    document["gates"][0]["note"] = "x"
    _parse_fails(_file(document))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", 2),
        ("schema", "1"),
        ("name", "Bad-Name"),
        ("name", "ab"),
        ("name", "csv_sum\n"),
        ("version", "1.0"),
        ("version", "1.0.0-beta"),
        ("version", "1." * 40 + "0"),
        ("code_sha", "A" * 64),
        ("code_sha", "a" * 63),
        ("blob_sha", "a" * 64 + "\n"),
        ("ledger_head", "g" * 64),
        ("toolforge_version", "bad version"),
        ("toolforge_version", ""),
        ("runtime_image_digest", "sha256:" + "e" * 63),
        ("runtime_image_digest", "e" * 64),
        ("minilocker_commit", "f" * 39),
        ("created_at", "yesterday"),
        ("created_at", "2026-11-26 12:00:00"),
        ("signer", "a" * 31),
        ("signer_key", "short="),
        ("signature", "a" * 86),
        ("gates", "none"),
        ("models", None),
        ("models", {"challenger": "has space", "builder": None}),
        ("models", {"challenger": "new\nline", "builder": None}),
        ("models", {"challenger": "x" * 121, "builder": None}),
        ("models", {"challenger": None}),
    ],
)
def test_every_field_has_a_fixed_shape(field: str, value: Any) -> None:
    document = _signed()
    document[field] = value
    _parse_fails(_file(document))


def test_gates_are_limited_in_number_shape_and_uniqueness() -> None:
    too_many = [{"id": f"G{n}", "status": "passed", "summary_sha": "1" * 64} for n in range(1, 14)]
    _parse_fails(_file(_signed(gates=too_many)))
    twice = [{"id": "G1", "status": "passed", "summary_sha": "1" * 64}] * 2
    _parse_fails(_file(_signed(gates=twice)))
    for gate in (
        {"id": "G0", "status": "passed", "summary_sha": "1" * 64},
        {"id": "G100", "status": "passed", "summary_sha": "1" * 64},
        {"id": "G1", "status": "ok", "summary_sha": "1" * 64},
        {"id": "G1", "status": "passed", "summary_sha": "1" * 63},
    ):
        _parse_fails(_file(_signed(gates=[gate])))


def test_types_are_not_coerced() -> None:
    _parse_fails(_file(_signed(schema=True)), "attestation_not_canonical")  # `true` is not 1
    document = _signed()
    document["schema"] = 1.0
    _parse_fails(_file(document), "attestation_not_canonical")
    document = _signed()
    document["gates"][0]["status"] = ["passed"]
    _parse_fails(_file(document))


@pytest.mark.parametrize(
    "data", [b"", b"[]", b"null", b"1", b'"x"', b"{", b"\xff\xfe", b'{"a": NaN}', b'{"a": Infinity}']
)
def test_something_that_is_not_a_json_object_is_refused(data: bytes) -> None:
    _parse_fails(data)


def test_a_deeply_nested_document_is_refused_not_crashed() -> None:
    _parse_fails(b"[" * 100_000 + b"]" * 100_000)


def test_a_large_attestation_is_refused_before_it_is_parsed() -> None:
    error = _parse_fails(b" " * (attest.MAX_ATTESTATION_BYTES + 1))
    assert "larger than" in error.message


def test_the_file_must_be_the_canonical_form_of_what_was_verified() -> None:
    """A signature over the parsed fields must not be able to vouch for a file that reads differently elsewhere."""
    document = _signed()
    canonical = _file(document).decode()
    pretty = json.dumps(document, indent=2).encode()
    _parse_fails(pretty, "attestation_not_canonical")
    _parse_fails(canonical.encode() + b"\n", "attestation_not_canonical")
    reordered = json.dumps(document, separators=(",", ":")).encode()  # insertion order, not sorted
    _parse_fails(reordered, "attestation_not_canonical")
    # A repeated key: Python keeps the last value, other readers keep the first.
    repeated = canonical.replace('"name":"csv_sum"', '"name":"evil_tool","name":"csv_sum"')
    assert repeated != canonical
    _parse_fails(repeated.encode(), "attestation_not_canonical")
    escaped = canonical.replace("csv_sum", "csv\\u005fsum")
    _parse_fails(escaped.encode(), "attestation_not_canonical")


@pytest.mark.parametrize(
    "field", ["name", "version", "toolforge_version", "created_at", "signer", "signer_key", "signature"]
)
def test_an_error_names_the_field_and_never_the_hostile_value(field: str) -> None:
    document = _signed()
    document[field] = HOSTILE
    error = _parse_fails(_file(document))
    assert field in error.message
    assert "EVIL" not in str(error) and "INJECTED" not in str(error) and "\x1b" not in str(error)


def test_a_hostile_value_in_a_model_id_is_not_echoed() -> None:
    error = _parse_fails(_file(_signed(models={"challenger": HOSTILE, "builder": None})))
    assert "models.challenger" in error.message and "INJECTED" not in str(error)


def test_an_error_lists_at_most_five_problems() -> None:
    document = _signed()
    for field in (
        "name",
        "version",
        "code_sha",
        "blob_sha",
        "ledger_head",
        "signer",
        "signature",
        "created_at",
    ):
        document[field] = "!"
    message = _parse_fails(_file(document)).message
    assert message.count(": ") <= 6


# --- What the ledger says about gates ---------------------------------------------------------------------------------------------


def _event(kind: str, gate: str | None, **payload: Any) -> dict[str, Any]:
    body = dict(payload) if gate is None else {"gate": gate, **payload}
    return {"type": kind, "payload": body, "hash": "0" * 64}


def test_gate_statements_take_the_last_outcome_of_each_gate_in_numeric_order() -> None:
    events = [
        _event("forge.gate.started", "G1"),
        _event("forge.gate.failed", "G3", summary="first try"),
        _event("forge.gate.passed", "G10", summary="ten"),
        _event("forge.gate.passed", "G2", summary="two"),
        _event("forge.gate.passed", "G3", summary="second try"),
        _event("forge.gate.skipped", "G5", summary="no model"),
    ]
    statements = attest.gate_statements(events)
    assert [(s["id"], s["status"]) for s in statements] == [
        ("G2", "passed"),
        ("G3", "passed"),
        ("G5", "skipped"),
        ("G10", "passed"),
    ]
    third = next(s for s in statements if s["id"] == "G3")
    assert third["summary_sha"] == attest.sha256_hex(
        attest.canonical_json({"gate": "G3", "summary": "second try"})
    )


def test_gate_statements_ignore_events_that_are_not_gate_outcomes() -> None:
    events: list[Any] = [
        {"type": "forge.submitted", "payload": {"gate": "G1"}},
        {"type": "forge.gate.passed"},
        {"type": "forge.gate.passed", "payload": "G1"},
        {"type": "forge.gate.passed", "payload": {"gate": 1}},
        {"type": "forge.gate.passed", "payload": {"gate": None}},
        {"type": None, "payload": {"gate": "G1"}},
        "not an event",
        42,
    ]
    assert attest.gate_statements(events) == []


def test_the_summary_hash_changes_when_anything_in_the_gate_payload_changes() -> None:
    one = attest.gate_statements([_event("forge.gate.passed", "G1", summary="a", metrics={"n": 1})])
    two = attest.gate_statements([_event("forge.gate.passed", "G1", summary="a", metrics={"n": 2})])
    assert one[0]["summary_sha"] != two[0]["summary_sha"]
