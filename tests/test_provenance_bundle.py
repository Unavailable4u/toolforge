"""Portable bundles (`toolforge.provenance.bundle`): export, verify and import against the real registry, the real hash-chained ledger and the
real pipeline with scripted gates. Each way a bundle can be wrong has its own test, its own error code, and a bundle built here on purpose."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest
from provenance_helpers import (
    FIXED_NOW,
    edited_json,
    export_to,
    make_bundle,
    make_forge,
    members_of,
    new_home,
    raw_tar,
    resigned,
    tree,
    verified,
    write_members,
)
from verify_helpers import ScriptedGate, scripted_gates, valid_package

from toolforge.errors import ProvenanceError, RegistryError, ToolForgeError
from toolforge.ledger import ToolforgeLedger
from toolforge.provenance import attest, bundle, keys
from toolforge.registry import Registry, lifecycle
from toolforge.services import builder
from toolforge.verify.pipeline import failed, passed, skipped

pytestmark = pytest.mark.usefixtures("minilocker")

LOCAL_SUMMARY = "checked on this machine"


def local_gates(**overrides: Any) -> list[ScriptedGate]:
    """The receiver's own gates: they say something different from what the origin's gates said, so a copy would show."""
    results = {g: passed(f"{LOCAL_SUMMARY}: {g}") for g in ("G1", "G2", "G3", "G4")}
    results.update(overrides)
    gates = scripted_gates()
    return [
        ScriptedGate(g.id, results[g.id], action=getattr(g, "action", None)) if g.id in results else g
        for g in gates
    ]


def fails(path: Path, settings: Any, code: str) -> ProvenanceError:
    with pytest.raises(ProvenanceError) as caught:
        bundle.verify_bundle(path, settings)
    assert caught.value.code == code, caught.value
    return caught.value


@pytest.fixture
def origin(tmp_path: Path):
    """`(settings, home, path)`: a home with a key and one exported version, csv_sum@1.0.0."""
    return make_bundle(tmp_path)


@pytest.fixture
def receiver(tmp_path: Path):
    """A fresh home that has never seen the bundle and has no signing key."""
    return new_home(tmp_path, "receiver")


def activate(registry: Registry, name: str, version: str) -> None:
    record = registry.add_version(valid_package(name=name, version=version), created_by="operator")
    for step in ("verifying", "verified", "active"):
        registry.transition(record.id, step, actor="pipeline")


# --- The round trip -----------------------------------------------------------------------------------------------------------


def test_export_verify_and_import_into_a_fresh_home(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    settings, home = receiver
    verified_bundle = bundle.verify_bundle(path, settings)
    assert verified_bundle.label == "csv_sum@1.0.0" and verified_bundle.signer.trust == keys.UNTRUSTED
    gates = local_gates()
    with Registry(home) as registry:
        forge = make_forge(settings, registry, gates)
        result = bundle.import_bundle(path, settings, registry, forge.submit_package)
        record = registry.find_version("csv_sum", "1.0.0")
    assert record is not None and record.status == lifecycle.VERIFIED_BASIC
    assert record.created_by == f"import:{verified_bundle.signer.fingerprint}"
    assert record.code_sha == verified_bundle.attestation.code_sha
    assert result.report.ok and not result.already_registered
    assert [g.calls for g in gates] == [1, 1, 1, 1]  # every local gate ran


def test_import_never_copies_what_the_bundle_says_about_gates(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    settings, home = receiver
    head = json.loads(members_of(path)["attestation.json"])["ledger_head"]
    with Registry(home) as registry:
        forge = make_forge(settings, registry, local_gates())
        bundle.import_bundle(path, settings, registry, forge.submit_package)
        record = registry.find_version("csv_sum", "1.0.0")
        rows = registry.gate_results(record.id)
    assert {row.gate for row in rows} >= {"G1", "G2", "G3", "G4"}
    for row in rows:
        summary = row.detail.get("summary", "")
        assert "is fine" not in summary  # the origin's gate text
        if row.gate in ("G1", "G2", "G3", "G4"):
            assert summary.startswith(LOCAL_SUMMARY)
    for file in home.rglob("*"):
        if file.is_file() and file.suffix != ".sqlite3":
            assert head.encode() not in file.read_bytes(), f"the origin's ledger head leaked into {file}"


def test_a_local_gate_that_fails_rejects_the_import_whatever_the_bundle_says(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    settings, home = receiver
    attested = {g.id: g.status for g in bundle.verify_bundle(path, settings).attestation.gates}
    assert attested["G3"] == "passed"
    with Registry(home) as registry:
        forge = make_forge(settings, registry, local_gates(G3=failed("fails here", ["fix it"])))
        result = bundle.import_bundle(path, settings, registry, forge.submit_package)
        record = registry.find_version("csv_sum", "1.0.0")
    assert not result.report.ok and record is not None and record.status == lifecycle.REJECTED


def test_a_skipped_local_gate_is_not_turned_into_a_pass_by_the_bundle(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    settings, home = receiver
    with Registry(home) as registry:
        forge = make_forge(settings, registry, local_gates(G3=skipped("cannot run here")))
        bundle.import_bundle(path, settings, registry, forge.submit_package)
        record = registry.find_version("csv_sum", "1.0.0")
        statuses = {row.gate: row.status for row in registry.gate_results(record.id)}
    assert statuses["G3"] == "skipped"


def test_a_trusted_signer_still_has_every_gate_run_and_nothing_is_promoted(
    tmp_path: Path, origin, receiver
) -> None:
    origin_settings, _, path = origin
    settings, home = receiver
    keys.trust_key(home, keys.keys_dir(origin_settings.home) / keys.PUBLIC_NAME, "origin")
    gates = local_gates(G4=failed("blocked locally"))
    with Registry(home) as registry:
        forge = make_forge(settings, registry, gates)
        result = bundle.import_bundle(path, settings, registry, forge.submit_package)
        assert result.verified.signer.trusted and result.verified.signer.label == "origin"
        assert [g.calls for g in gates] == [1, 1, 1, 1]
        assert not result.report.ok
        assert registry.active_version("csv_sum") is None


def test_an_import_is_never_promoted(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    settings, home = receiver
    with Registry(home) as registry:
        forge = make_forge(settings, registry, local_gates())
        bundle.import_bundle(path, settings, registry, forge.submit_package)
        assert registry.active_version("csv_sum") is None
        assert registry.find_version("csv_sum", "1.0.0").status == lifecycle.VERIFIED_BASIC


def test_a_tool_that_asks_for_the_network_still_waits_for_the_operators_approval(
    tmp_path: Path, receiver
) -> None:
    settings_o, home_o = new_home(tmp_path, "origin")
    keys.init_keys(home_o)
    package = valid_package(
        name="fetcher",
        version="1.0.0",
        permissions={"network": ["api.example.com"], "secrets": [], "inputs": False, "browser": False},
    )
    with Registry(home_o) as registry:
        forge = make_forge(settings_o, registry)
        forge.submit_package(package, "operator")
        record = registry.find_version("fetcher", "1.0.0")
        assert record.status == lifecycle.PENDING_APPROVAL  # exportable, and not yet approved
        path = tmp_path / "fetcher.tfbundle"
        export_to(settings_o, registry, record, path)
    settings, home = receiver
    with Registry(home) as registry:
        forge = make_forge(settings, registry)
        result = bundle.import_bundle(path, settings, registry, forge.submit_package)
        record = registry.find_version("fetcher", "1.0.0")
        assert record.status == lifecycle.PENDING_APPROVAL and result.report.ok
        approvals = registry.list_approvals()
    assert [(a.status) for a in approvals] == ["pending"]  # the operator here has not approved anything


def test_importing_the_same_bundle_again_runs_nothing_and_says_so(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    settings, home = receiver
    gates = local_gates()
    with Registry(home) as registry:
        forge = make_forge(settings, registry, gates)
        bundle.import_bundle(path, settings, registry, forge.submit_package)
        again = bundle.import_bundle(path, settings, registry, forge.submit_package)
    assert again.already_registered and "already registered" in again.text()
    assert [g.calls for g in gates] == [1, 1, 1, 1]


def test_a_bundle_cannot_replace_a_version_that_exists_with_other_content(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    settings, home = receiver
    with Registry(home) as registry:
        forge = make_forge(settings, registry)
        verified(
            settings,
            registry,
            valid_package(name="csv_sum", version="1.0.0", description="A different tool with this name."),
        )
        with pytest.raises(RegistryError) as caught:
            bundle.import_bundle(path, settings, registry, forge.submit_package)
    assert caught.value.code == "version_exists"


def test_a_bundle_cannot_take_the_name_of_an_active_tool(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    settings, home = receiver
    with Registry(home) as registry:
        activate(registry, "csv_sum", "0.5.0")
        forge = make_forge(settings, registry)
        with pytest.raises(RegistryError) as caught:
            bundle.import_bundle(path, settings, registry, forge.submit_package)
        assert registry.find_version("csv_sum", "1.0.0") is None
    assert caught.value.code == "name_collision"


def test_a_bundle_that_does_not_verify_registers_nothing(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = members_of(path)
    blob = bytearray(members["package.tar.gz"])
    blob[len(blob) // 2] ^= 0x01
    members["package.tar.gz"] = bytes(blob)
    broken = write_members(tmp_path / "broken.tfbundle", members)
    settings, home = receiver
    gates = local_gates()
    with Registry(home) as registry:
        forge = make_forge(settings, registry, gates)
        with pytest.raises(ProvenanceError):
            bundle.import_bundle(broken, settings, registry, forge.submit_package)
        assert registry.list_versions() == []
    assert [g.calls for g in gates] == [0, 0, 0, 0]


# --- Verifying changes nothing ------------------------------------------------------------------------------------------------------


def test_verify_bundle_changes_nothing_and_needs_no_key_or_registry(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    settings, home = receiver
    before, bytes_before = tree(home), path.read_bytes()
    result = bundle.verify_bundle(path, settings)
    assert tree(home) == before == {}  # no registry, no ledger, no key was created
    assert path.read_bytes() == bytes_before
    assert result.package.manifest.name == "csv_sum"


def test_every_check_that_ran_is_listed(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    checks = bundle.verify_bundle(path, receiver[0]).checks
    assert len(checks) == 7
    text = " ".join(checks)
    for word in (
        "six expected files",
        "blob_sha",
        "code_sha",
        "hash chain",
        "ledger_head",
        "gate outcomes",
        "signature",
    ):
        assert word in text


# --- Who signed it --------------------------------------------------------------------------------------------------------------


def test_an_unknown_signer_is_described_as_unsigned_by_anyone_you_trust_and_is_still_importable(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    settings, home = receiver
    result = bundle.verify_bundle(path, settings)
    assert not result.signer.trusted
    assert (
        "unsigned by anyone you trust" in result.signer.describe()
        and "unsigned by anyone you trust" in result.text()
    )
    assert (
        result.as_dict()["signed_by_trusted_key"] is False
        and result.as_dict()["signer"]["trust"] == "untrusted"
    )
    with Registry(home) as registry:
        forge = make_forge(settings, registry, local_gates())
        assert bundle.import_bundle(path, settings, registry, forge.submit_package).report.ok


def test_trust_changes_how_the_signer_is_described_and_untrust_changes_it_back(
    tmp_path: Path, origin, receiver
) -> None:
    origin_settings, _, path = origin
    settings, home = receiver
    public = keys.keys_dir(origin_settings.home) / keys.PUBLIC_NAME
    keys.trust_key(home, public, "alice")
    trusted = bundle.verify_bundle(path, settings)
    assert trusted.signer.trust == keys.TRUSTED and trusted.signer.label == "alice"
    assert "a key you trust" in trusted.text() and trusted.as_dict()["signed_by_trusted_key"] is True
    keys.untrust_key(home, trusted.signer.fingerprint)
    assert bundle.verify_bundle(path, settings).signer.trust == keys.UNTRUSTED


def test_the_operators_own_signature_is_described_as_their_own(tmp_path: Path, origin) -> None:
    origin_settings, _, path = origin
    result = bundle.verify_bundle(path, origin_settings)
    assert result.signer.trust == keys.OWN and "your own key" in result.signer.describe()


def test_a_stranger_can_make_a_bundle_that_verifies_and_that_is_why_trust_is_a_separate_question(
    tmp_path: Path, origin, receiver
) -> None:
    """Whoever holds a key can sign anything consistent. The signature proves origin, never safety."""
    origin_settings, _, path = origin
    settings, _ = receiver
    forged = resigned(origin_settings, members_of(path), created_at="2020-01-01T00:00:00Z")
    forged_path = write_members(tmp_path / "forged.tfbundle", forged)
    result = bundle.verify_bundle(forged_path, settings)
    assert not result.signer.trusted
    assert "does not show the tool is safe" in result.text()


# --- One failure, one code ------------------------------------------------------------------------------------------------------


def test_a_changed_package_fails_blob_sha(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = members_of(path)
    blob = bytearray(members["package.tar.gz"])
    blob[len(blob) // 2] ^= 0x01
    members["package.tar.gz"] = bytes(blob)
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "blob_sha_mismatch")


def test_changed_code_with_a_matching_blob_sha_fails_code_sha(tmp_path: Path, origin, receiver) -> None:
    origin_settings, _, path = origin
    members = members_of(path)
    package = valid_package(name="csv_sum", version="1.0.0")
    package.files["tool.py"] += "\n# a change after signing\n"
    evil = package.to_tar()
    members = resigned(
        origin_settings, {**members, "package.tar.gz": evil}, blob_sha=hashlib.sha256(evil).hexdigest()
    )
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "code_sha_mismatch")


def test_a_manifest_that_differs_from_the_one_in_the_package_fails(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = edited_json(
        members_of(path), "manifest.json", description="A description the package never had, long enough."
    )
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "manifest_mismatch")


@pytest.mark.parametrize("member", ["bundle.json", "manifest.json"])
def test_parts_that_name_another_tool_or_version_fail(tmp_path: Path, origin, receiver, member: str) -> None:
    _, _, path = origin
    for change in ({"name": "other_tool"}, {"version": "9.9.9"}):
        members = edited_json(members_of(path), member, **change)
        fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "bundle_inconsistent")


def test_bundle_json_must_have_the_expected_shape(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = members_of(path)
    for header in (
        b"not json",
        b"[]",
        b'{"schema":2}',
        attest.canonical_json({**json.loads(members["bundle.json"]), "extra": 1}),
    ):
        fails(
            write_members(tmp_path / "t.tfbundle", {**members, "bundle.json": header}),
            receiver[0],
            "bundle_invalid",
        )


@pytest.mark.parametrize("member", ["manifest.json", "report.json"])
def test_manifest_and_report_must_be_json_objects(tmp_path: Path, origin, receiver, member: str) -> None:
    _, _, path = origin
    for body in (b"not json", b"[1, 2]", b"\xff\xfe"):
        members = {**members_of(path), member: body}
        fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "bundle_invalid")


def _ledger_lines(members: dict[str, bytes]) -> list[str]:
    return members["ledger.jsonl"].decode("ascii").splitlines()


def test_an_edited_ledger_line_fails_the_hash_chain(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = members_of(path)
    lines = _ledger_lines(members)
    index = next(i for i, line in enumerate(lines) if "forge.gate.passed" in line)
    event = json.loads(lines[index])
    event["payload"]["summary"] = "this gate was edited after it ran"
    lines[index] = json.dumps(event, sort_keys=True, separators=(",", ":"))
    members["ledger.jsonl"] = ("\n".join(lines) + "\n").encode()
    error = fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_chain_broken")
    assert "do not import" in str(error)


def test_reordered_ledger_events_fail_the_hash_chain(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = members_of(path)
    lines = _ledger_lines(members)
    lines[2], lines[3] = lines[3], lines[2]
    members["ledger.jsonl"] = ("\n".join(lines) + "\n").encode()
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_chain_broken")


def test_a_ledger_with_an_event_removed_from_the_middle_fails_the_hash_chain(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    members = members_of(path)
    lines = _ledger_lines(members)
    del lines[4]
    members["ledger.jsonl"] = ("\n".join(lines) + "\n").encode()
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_chain_broken")


def test_a_ledger_cut_short_still_chains_but_is_not_the_signed_head(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = members_of(path)
    members["ledger.jsonl"] = ("\n".join(_ledger_lines(members)[:-1]) + "\n").encode()
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_head_mismatch")


def test_a_signed_statement_with_another_ledger_head_fails_ledger_head(
    tmp_path: Path, origin, receiver
) -> None:
    origin_settings, _, path = origin
    members = resigned(origin_settings, members_of(path), ledger_head="0" * 64)
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_head_mismatch")


def test_a_ledger_from_another_tool_fails_ledger_mismatch(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    _, _, other = make_bundle(tmp_path / "second", name="word_tally")
    members = {**members_of(path), "ledger.jsonl": members_of(other)["ledger.jsonl"]}
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_mismatch")


def test_a_ledger_that_is_the_same_tool_with_other_code_fails_ledger_mismatch(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    _, _, other = make_bundle(
        tmp_path / "second", description="The same tool name with different content here."
    )
    members = {**members_of(path), "ledger.jsonl": members_of(other)["ledger.jsonl"]}
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_mismatch")


def test_attested_gate_outcomes_must_be_the_ones_the_ledger_records(tmp_path: Path, origin, receiver) -> None:
    origin_settings, _, path = origin
    members = members_of(path)
    gates = json.loads(members["attestation.json"])["gates"]
    wrong_status = [{**g, "status": "failed" if g["id"] == "G1" else g["status"]} for g in gates]
    wrong_hash = [{**g, "summary_sha": "0" * 64} if g["id"] == "G2" else g for g in gates]
    for claimed in (
        wrong_status,
        wrong_hash,
        gates[:-1],
        gates + [{"id": "G9", "status": "passed", "summary_sha": "1" * 64}],
    ):
        forged = resigned(origin_settings, members, gates=claimed)
        fails(write_members(tmp_path / "t.tfbundle", forged), receiver[0], "gates_mismatch")


@pytest.mark.parametrize(
    "ledger",
    [b"\xff\xfe\x00", b"this is not json\n", b"[1,2,3]\n", b'{"a": NaN}\n'],
)
def test_a_ledger_that_is_not_a_list_of_json_objects_fails(
    tmp_path: Path, origin, receiver, ledger: bytes
) -> None:
    _, _, path = origin
    members = {**members_of(path), "ledger.jsonl": ledger}
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_invalid")


def test_an_empty_ledger_verifies_nothing(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = {**members_of(path), "ledger.jsonl": b"\n\n"}
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "ledger_chain_broken")


def test_a_ledger_with_too_many_events_is_refused(
    tmp_path: Path, origin, receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, path = origin
    monkeypatch.setattr(bundle, "MAX_LEDGER_EVENTS", 3)
    fails(path, receiver[0], "ledger_invalid")


def test_a_swapped_signer_fingerprint_fails_signer_mismatch(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = edited_json(members_of(path), "attestation.json", signer="0" * 32)
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "signer_mismatch")


def test_a_flipped_signature_byte_fails_signature_invalid(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    members = members_of(path)
    raw = bytearray(base64.b64decode(json.loads(members["attestation.json"])["signature"]))
    raw[0] ^= 0x80
    flipped = edited_json(members, "attestation.json", signature=base64.b64encode(bytes(raw)).decode())
    fails(write_members(tmp_path / "t.tfbundle", flipped), receiver[0], "signature_invalid")


def test_a_signature_replaced_by_another_keys_fails_even_with_its_fingerprint(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    _, other_home, other_path = make_bundle(tmp_path / "second", name="word_tally")
    other = json.loads(members_of(other_path)["attestation.json"])
    members = edited_json(
        members_of(path), "attestation.json", signer=other["signer"], signer_key=other["signer_key"]
    )
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "signature_invalid")


def test_an_attestation_that_was_written_again_fails_even_if_it_still_means_the_same(
    tmp_path: Path, origin, receiver
) -> None:
    _, _, path = origin
    members = members_of(path)
    members["attestation.json"] = json.dumps(json.loads(members["attestation.json"]), indent=2).encode()
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "attestation_not_canonical")


def test_a_bundle_whose_attestation_is_missing_fields_fails(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    document = json.loads(members_of(path)["attestation.json"])
    del document["ledger_head"]
    members = {**members_of(path), "attestation.json": attest.canonical_json(document)}
    fails(write_members(tmp_path / "t.tfbundle", members), receiver[0], "attestation_invalid")


# --- The frozen package -----------------------------------------------------------------------------------------------------------


def _with_blob(origin_settings: Any, path: Path, blob: bytes) -> dict[str, bytes]:
    return resigned(
        origin_settings,
        {**members_of(path), "package.tar.gz": blob},
        blob_sha=hashlib.sha256(blob).hexdigest(),
    )


def _entries(package: Any) -> dict[str, bytes]:
    """The files of a package archive by name, manifest included."""
    with tarfile.open(fileobj=io.BytesIO(package.to_tar())) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive if m.isreg()}


@pytest.mark.parametrize(
    "extra",
    [
        ("link", b"", tarfile.SYMTYPE),
        ("hard", b"", tarfile.LNKTYPE),
        ("../escape.py", b"x = 1\n", tarfile.REGTYPE),
        ("/abs.py", b"x = 1\n", tarfile.REGTYPE),
        ("binary.py", b"\xff\xfe\x00", tarfile.REGTYPE),
        ("nul.py", b"x = 1\x00\n", tarfile.REGTYPE),
    ],
)
def test_a_frozen_package_that_the_runner_would_refuse_fails_blob_unusable(
    tmp_path: Path, origin, receiver, extra
) -> None:
    origin_settings, _, path = origin
    entries = _entries(valid_package(name="csv_sum", version="1.0.0"))
    blob = raw_tar([(name, data, tarfile.REGTYPE) for name, data in entries.items()] + [extra])
    fails(
        write_members(tmp_path / "t.tfbundle", _with_blob(origin_settings, path, blob)),
        receiver[0],
        "blob_unusable",
    )


def test_a_frozen_package_that_is_not_an_archive_fails_blob_unusable(
    tmp_path: Path, origin, receiver
) -> None:
    origin_settings, _, path = origin
    fails(
        write_members(tmp_path / "t.tfbundle", _with_blob(origin_settings, path, b"not an archive")),
        receiver[0],
        "blob_unusable",
    )


def test_a_frozen_package_without_a_manifest_fails_blob_unusable(tmp_path: Path, origin, receiver) -> None:
    origin_settings, _, path = origin
    blob = raw_tar([("tool.py", b"x = 1\n", tarfile.REGTYPE)])
    fails(
        write_members(tmp_path / "t.tfbundle", _with_blob(origin_settings, path, blob)),
        receiver[0],
        "blob_unusable",
    )


def test_a_frozen_workspace_with_vendored_dependencies_verifies(tmp_path: Path, origin, receiver) -> None:
    """G2's real blob is `pkg/...` with `pkg/.deps`: the vendored libraries are not part of `code_sha`, the package files are."""
    origin_settings, _, path = origin
    entries = _entries(valid_package(name="csv_sum", version="1.0.0"))
    placed = [(f"pkg/{name}", data, tarfile.REGTYPE) for name, data in entries.items()]
    placed.append(("pkg/.deps/somelib/__init__.py", b"VALUE = 1\n", tarfile.REGTYPE))
    placed.append(("pkg/.deps/somelib/data.bin", b"\xff\x00\xfe", tarfile.REGTYPE))
    result = bundle.verify_bundle(
        write_members(tmp_path / "t.tfbundle", _with_blob(origin_settings, path, raw_tar(placed))),
        receiver[0],
    )
    assert result.package.code_sha == result.attestation.code_sha


def test_the_rebuilt_package_equals_the_one_that_was_exported(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    result = bundle.verify_bundle(path, receiver[0])
    original = valid_package(name="csv_sum", version="1.0.0")
    assert result.package.files == original.files
    assert result.package.manifest.model_dump(mode="json") == original.manifest.model_dump(mode="json")


# --- The archive ---------------------------------------------------------------------------------------------------------------


def _read(data: bytes) -> ProvenanceError:
    with pytest.raises(ProvenanceError) as caught:
        bundle.read_members(data)
    return caught.value


def _good_entries(path: Path) -> list[tuple[str, bytes, bytes]]:
    return [(name, body, tarfile.REGTYPE) for name, body in members_of(path).items()]


@pytest.mark.parametrize("name", ["../evil.py", "sub/../evil.py", "/etc/passwd", "..\\evil.py", "a\\b"])
def test_a_member_with_an_unsafe_path_refuses_the_whole_bundle(tmp_path: Path, origin, name: str) -> None:
    _, _, path = origin
    error = _read(raw_tar(_good_entries(path) + [(name, b"x", tarfile.REGTYPE)]))
    assert error.code == "bundle_member_unsafe" and "do not import" in str(error)


def test_an_unsafe_name_is_shown_cleaned_and_clipped(tmp_path: Path, origin) -> None:
    _, _, path = origin
    error = _read(raw_tar(_good_entries(path) + [("../\x1b[31m" + "x" * 300, b"x", tarfile.REGTYPE)]))
    assert error.code == "bundle_member_unsafe" and "\x1b" not in str(error) and len(str(error)) < 400


def test_a_member_that_is_not_part_of_the_format_refuses_the_bundle(tmp_path: Path, origin) -> None:
    _, _, path = origin
    error = _read(raw_tar(_good_entries(path) + [("extra.txt", b"x", tarfile.REGTYPE)]))
    assert error.code == "bundle_member_unknown" and "attestation.json" in str(error)


def test_a_repeated_member_refuses_the_bundle(tmp_path: Path, origin) -> None:
    _, _, path = origin
    error = _read(raw_tar(_good_entries(path) + [("bundle.json", b"{}", tarfile.REGTYPE)]))
    assert error.code == "bundle_member_duplicate"


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE])
def test_a_member_that_is_not_a_plain_file_refuses_the_bundle(tmp_path: Path, origin, kind: bytes) -> None:
    _, _, path = origin
    entries = [e for e in _good_entries(path) if e[0] != "report.json"] + [("report.json", b"", kind)]
    assert _read(raw_tar(entries)).code == "bundle_member_unsafe"


def test_a_missing_member_is_named(tmp_path: Path, origin) -> None:
    _, _, path = origin
    entries = [e for e in _good_entries(path) if e[0] not in ("ledger.jsonl", "report.json")]
    error = _read(raw_tar(entries))
    assert (
        error.code == "bundle_member_missing" and "ledger.jsonl" in str(error) and "report.json" in str(error)
    )


def test_a_member_over_its_limit_refuses_the_bundle(
    tmp_path: Path, origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, path = origin
    monkeypatch.setitem(bundle.MEMBER_LIMITS, "report.json", 10)
    assert _read(path.read_bytes()).code == "bundle_member_too_large"


def test_a_bundle_over_the_size_cap_is_refused_before_it_is_read(
    tmp_path: Path, origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, path = origin
    monkeypatch.setattr(bundle, "MAX_BUNDLE_BYTES", 1000)
    assert _read(path.read_bytes()).code == "bundle_too_large"
    with pytest.raises(ProvenanceError) as caught:
        bundle.read_bundle_file(path)
    assert caught.value.code == "bundle_too_large"


@pytest.mark.parametrize("kind", ["empty", "random", "text", "gzip", "truncated", "zip-like"])
def test_something_that_is_not_an_uncompressed_tar_is_not_a_bundle(tmp_path: Path, origin, kind: str) -> None:
    _, _, path = origin
    data = {
        "empty": b"",
        "random": os.urandom(4096),
        "text": b"hello, I am a bundle\n" * 100,
        "gzip": gzip.compress(path.read_bytes()),
        "truncated": path.read_bytes()[:700],
        "zip-like": b"PK\x03\x04" + b"\x00" * 100,
    }[kind]
    assert _read(data).code in ("bundle_unreadable", "bundle_member_missing")


def test_a_bundle_file_must_be_a_plain_file(tmp_path: Path, origin) -> None:
    _, _, path = origin
    with pytest.raises(ProvenanceError) as caught:
        bundle.read_bundle_file(tmp_path / "missing.tfbundle")
    assert caught.value.code == "bundle_missing"
    with pytest.raises(ProvenanceError) as caught:
        bundle.read_bundle_file(tmp_path)
    assert caught.value.code == "bundle_unreadable"
    link = tmp_path / "link.tfbundle"
    link.symlink_to(path)
    with pytest.raises(ProvenanceError) as caught:
        bundle.read_bundle_file(link)
    assert caught.value.code == "bundle_unreadable"


def test_nothing_is_extracted_to_disk_when_a_bundle_is_read(tmp_path: Path, origin, receiver) -> None:
    _, _, path = origin
    before = {p for p in tmp_path.rglob("*")}
    bundle.verify_bundle(path, receiver[0])
    assert {p for p in tmp_path.rglob("*")} == before


# --- Exporting ------------------------------------------------------------------------------------------------------------------


def test_a_bundle_holds_exactly_six_plain_files(tmp_path: Path, origin) -> None:
    _, _, path = origin
    with tarfile.open(path) as archive:
        members = archive.getmembers()
    assert sorted(m.name for m in members) == sorted(bundle.MEMBER_LIMITS)
    assert all(m.isreg() and m.mtime == 0 and m.uid == 0 for m in members)


def test_the_package_in_the_bundle_is_the_registrys_blob_byte_for_byte(tmp_path: Path, origin) -> None:
    settings, home, path = origin
    with Registry(home) as registry:
        record = registry.find_version("csv_sum", "1.0.0")
        assert members_of(path)["package.tar.gz"] == registry.blobs.get(record.blob_sha)
        assert hashlib.sha256(members_of(path)["package.tar.gz"]).hexdigest() == record.blob_sha


def test_exporting_twice_gives_the_same_bytes(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry)
        export_to(settings, registry, record, tmp_path / "one.tfbundle")
        export_to(settings, registry, record, tmp_path / "two.tfbundle")
    assert (tmp_path / "one.tfbundle").read_bytes() == (tmp_path / "two.tfbundle").read_bytes()


def test_exporting_changes_neither_the_registry_nor_the_ledgers(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry)
        ledgers, blobs = tree(home / "ledgers"), tree(home / "blobs")
        states = [(v.name, v.version, v.status, v.updated_at) for v in registry.list_versions()]
        export_to(settings, registry, record, tmp_path / "x.tfbundle")
        assert tree(home / "ledgers") == ledgers and tree(home / "blobs") == blobs
        assert [(v.name, v.version, v.status, v.updated_at) for v in registry.list_versions()] == states


def test_the_private_key_is_in_no_bundle_and_no_export_message(tmp_path: Path, origin) -> None:
    settings, home, path = origin
    seed = (keys.keys_dir(home) / keys.PRIVATE_NAME).read_text().split()[1]
    raw_seed = base64.b64decode(seed)
    data = path.read_bytes()
    assert seed.encode() not in data and raw_seed not in data
    with Registry(home) as registry:
        result = export_to(
            settings, registry, registry.find_version("csv_sum", "1.0.0"), tmp_path / "again.tfbundle"
        )
    assert seed not in result.text() and seed not in repr(result)


def test_export_names_what_it_wrote_and_how_to_check_it(tmp_path: Path, origin) -> None:
    settings, home, path = origin
    with Registry(home) as registry:
        result = export_to(
            settings,
            registry,
            registry.find_version("csv_sum", "1.0.0"),
            tmp_path / "t.tfbundle",
            force=False,
        )
    assert (
        result.label == "csv_sum@1.0.0"
        and result.sha256 == hashlib.sha256(Path(result.path).read_bytes()).hexdigest()
    )
    assert "verify-bundle" in result.text() and result.signer in result.text()


def test_export_refuses_without_a_signing_key(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "keyless")
    with Registry(home) as registry:
        record = verified(settings, registry)
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert caught.value.code == "no_signing_key" and not (tmp_path / "x.tfbundle").exists()


def test_export_refuses_an_unknown_version_and_a_bad_reference(tmp_path: Path, origin) -> None:
    settings, home, _ = origin
    with Registry(home) as registry:
        with pytest.raises(ProvenanceError) as caught:
            bundle.export_bundle(settings, registry, "nothing@1.0.0", tmp_path / "x.tfbundle")
        assert caught.value.code == "unknown_version"
        with pytest.raises(ToolForgeError):
            bundle.export_bundle(settings, registry, "no-version-given", tmp_path / "x.tfbundle")


def _in_state(registry: Registry, settings: Any, status: str):
    """A registered version in the given state, moved only along the lifecycle's own edges."""
    if status == lifecycle.QUARANTINED:
        record = verified(settings, registry, valid_package(name="was_active", version="1.0.0"))
        registry.transition(record.id, "active", actor="operator", reason="test")
        registry.transition(record.id, "quarantined", actor="forge")
        return registry.get_version(record.id)
    record = registry.add_version(valid_package(name="not_yet", version="1.0.0"), created_by="operator")
    registry.transition(record.id, "verifying", actor="pipeline")
    if status == lifecycle.REJECTED:
        registry.transition(record.id, "rejected", actor="pipeline")
    return registry.get_version(record.id)


@pytest.mark.parametrize("status", [lifecycle.REJECTED, lifecycle.QUARANTINED, lifecycle.VERIFYING])
def test_only_a_version_that_passed_verification_is_exported(tmp_path: Path, status: str) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = _in_state(registry, settings, status)
        assert record.status == status
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert caught.value.code == "not_exportable" and not (tmp_path / "x.tfbundle").exists()


def test_a_version_that_was_never_frozen_is_not_exported(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        forge = make_forge(settings, registry, scripted_gates(blob=False))
        forge.submit_package(valid_package(), "operator")
        record = registry.find_version("my_tool", "0.1.0")
        assert record.status in (lifecycle.VERIFIED_BASIC, lifecycle.VERIFIED) and record.blob_sha is None
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert caught.value.code == "no_blob"


def test_a_version_without_a_verification_ledger_has_no_history_to_export(tmp_path: Path, origin) -> None:
    settings, home, _ = origin
    shutil.rmtree(home / "ledgers" / "csv_sum@1.0.0")
    with Registry(home) as registry:
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, registry.find_version("csv_sum", "1.0.0"), tmp_path / "x.tfbundle")
    assert caught.value.code == "no_ledger"


def test_a_damaged_ledger_blocks_the_export(tmp_path: Path, origin) -> None:
    settings, home, _ = origin
    ledger = next((home / "ledgers" / "csv_sum@1.0.0").glob("verify-*.jsonl"))
    lines = ledger.read_text().splitlines()
    lines[3] = "{ this line is damaged"
    ledger.write_text("\n".join(lines) + "\n")
    with Registry(home) as registry:
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, registry.find_version("csv_sum", "1.0.0"), tmp_path / "x.tfbundle")
    assert caught.value.code == "ledger_invalid" and "do not edit ledger files" in str(caught.value)


def test_the_newest_ledger_that_holds_gate_events_is_the_one_exported(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry)
        folder = home / "ledgers" / record.label
        older = next(folder.glob("verify-*.jsonl"))
        # A later run that only opened an approval request: it has no gate events and must be skipped.
        newer = folder / "verify-zz-later.jsonl"
        ledger = ToolforgeLedger(str(newer), "verify:later")
        ledger.append("forge", "forge.submitted", {"tool": record.label, "code_sha": record.code_sha})
        os.utime(newer, (older.stat().st_atime + 100, older.stat().st_mtime + 100))
        path, events = bundle.select_verification_ledger(settings, record.label)
    assert path == older and any(e["type"] == "forge.gate.passed" for e in events)


def test_the_stored_blast_radius_report_is_used_when_there_is_one(tmp_path: Path) -> None:
    from toolforge.report import blast_radius

    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry)
        blast_radius.write(settings, record.label, {"marker": "stored report", "tool": record.name})
        export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert json.loads(members_of(tmp_path / "x.tfbundle")["report.json"])["marker"] == "stored report"


def test_the_output_file_is_never_replaced_without_force(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    target = tmp_path / "x.tfbundle"
    target.write_bytes(b"precious")
    with Registry(home) as registry:
        record = verified(settings, registry)
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, record, target)
        assert caught.value.code == "output_exists" and target.read_bytes() == b"precious"
        export_to(settings, registry, record, target, force=True)
    assert (
        target.read_bytes() != b"precious" and bundle.verify_bundle(target, settings).label == "csv_sum@1.0.0"
    )
    assert [p.name for p in tmp_path.glob(".tmp-*")] == []  # no temporary file is left behind


def test_an_unwritable_output_folder_is_explained(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry)
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, record, tmp_path / "no-such-folder" / "x.tfbundle")
    assert caught.value.code == "output_unwritable"


def test_a_damaged_blob_in_the_registry_blocks_the_export(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry)
        blob = next((home / "blobs").rglob(f"{record.blob_sha}*"))
        blob.write_bytes(blob.read_bytes() + b"tampered")
        with pytest.raises(RegistryError) as caught:
            export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert caught.value.code == "blob_corrupt" and not (tmp_path / "x.tfbundle").exists()


# --- What is recorded about how the version was made ----------------------------------------------------------------------------------


def test_the_challenger_model_comes_from_the_g5_metrics_in_the_ledger(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    gates = scripted_gates() + [
        ScriptedGate("G5", passed("challenged", metrics={"model": "vendor/model-1:free"}))
    ]
    with Registry(home) as registry:
        record = verified(settings, registry, gates=gates)
        export_to(settings, registry, record, tmp_path / "x.tfbundle")
    result = bundle.verify_bundle(tmp_path / "x.tfbundle", settings)
    assert (
        result.attestation.models.challenger == "vendor/model-1:free"
        and result.attestation.models.builder is None
    )
    assert "vendor/model-1:free" in result.text()


@pytest.mark.parametrize("model", [None, "has a space", "new\nline", "x" * 121, 42, ""])
def test_a_challenger_model_that_is_not_a_safe_id_is_not_recorded(model: Any) -> None:
    events = [{"type": "forge.gate.passed", "payload": {"gate": "G5", "metrics": {"model": model}}}]
    assert bundle._challenger_model(events) is None


def test_a_skipped_g5_records_no_challenger() -> None:
    events = [{"type": "forge.gate.skipped", "payload": {"gate": "G5", "metrics": {"model": None}}}]
    assert bundle._challenger_model(events) is None
    assert bundle._challenger_model([]) is None


def test_the_builder_model_comes_from_the_build_ledger_for_versions_the_builder_made(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry, by=builder.ACTOR)
        ledger = ToolforgeLedger(
            str(home / "ledgers" / builder.BUILD_LEDGER_DIR / "build-1.jsonl"), "build-1"
        )
        ledger.append(
            "forge",
            builder.EVENT,
            {"tool": record.name, "versions": [record.version], "model": "vendor/builder-2:free"},
        )
        assert bundle._builder_model(settings, record) == "vendor/builder-2:free"
        export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert (
        bundle.verify_bundle(tmp_path / "x.tfbundle", settings).attestation.models.builder
        == "vendor/builder-2:free"
    )


def test_a_version_an_operator_submitted_has_no_builder_model(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    with Registry(home) as registry:
        record = verified(settings, registry, by="operator")
        ledger = ToolforgeLedger(
            str(home / "ledgers" / builder.BUILD_LEDGER_DIR / "build-1.jsonl"), "build-1"
        )
        ledger.append(
            "forge", builder.EVENT, {"tool": record.name, "versions": [record.version], "model": "vendor/x"}
        )
        assert bundle._builder_model(settings, record) is None


def test_the_runtime_image_digest_is_recorded_only_when_it_is_a_real_sha256(tmp_path: Path) -> None:
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    with Registry(home) as registry:
        record = verified(settings, registry)
        for lookup, expected in (
            (lambda _: "sha256:" + "9" * 64, "sha256:" + "9" * 64),
            (lambda _: "sha256:abc", None),
            (lambda _: "9" * 64, None),
            (lambda _: None, None),
        ):
            export_to(settings, registry, record, tmp_path / "x.tfbundle", force=True, image_lookup=lookup)
            assert (
                bundle.verify_bundle(tmp_path / "x.tfbundle", settings).attestation.runtime_image_digest
                == expected
            )


def test_the_provenance_is_marked_informational_and_the_text_has_no_control_characters(
    tmp_path: Path, origin
) -> None:
    settings, _, path = origin
    result = bundle.verify_bundle(path, settings)
    document = result.as_dict()
    assert document["provenance"]["informational_only"] is True and document["ok"] is True
    assert document["provenance"]["gates_as_recorded_by_signer"][0]["id"] == "G1"
    assert (
        document["provenance"]["minilocker_commit"]
        == attest.parse(members_of(path)["attestation.json"]).minilocker_commit
    )
    assert json.loads(json.dumps(document)) == document
    text = result.text()
    assert not [c for c in text if ord(c) < 32 and c != "\n"]
    assert FIXED_NOW in text


def test_export_uses_the_clock_it_is_given(tmp_path: Path, origin) -> None:
    settings, _, path = origin
    assert bundle.verify_bundle(path, settings).attestation.created_at == FIXED_NOW
    assert json.loads(members_of(path)["bundle.json"])["created_at"] == FIXED_NOW


def test_the_signing_key_is_used_by_export_and_nothing_else_in_the_package() -> None:
    """`load_private_key` is called from `export_bundle` only, so the operator's private key has one way out of its file: into a signature.

    The one other place is the attack lab's `Stranger` (P15), which is somebody else's ToolForge: it makes its own throwaway key in its own
    temporary directory and signs with that, so the lab can hand the receiver bundles a hostile author would sign. It is named here, by class
    and method, so that a call from anywhere else in the package, or from another method of `Stranger`, still fails this test.
    `test_the_attack_labs_stranger_never_holds_the_operators_key` checks that its key is not the operator's.
    """
    import ast

    root = Path(bundle.__file__).parent.parent
    callers = set()
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        owner = {
            method: cls.name
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            for method in cls.body
            if isinstance(method, ast.FunctionDef)
        }
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            name = f"{owner[function]}.{function.name}" if function in owner else function.name
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    callee = (
                        node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", "")
                    )
                    if callee in ("load_private_key", "sign"):
                        callers.add((source.relative_to(root).as_posix(), name, callee))
    assert callers == {
        ("provenance/bundle.py", "export_bundle", "load_private_key"),
        ("provenance/bundle.py", "export_bundle", "sign"),
        ("provenance/attest.py", "sign", "sign"),  # `attest.sign` itself calls the key's own `.sign`
        (
            "attacks/runner.py",
            "Stranger.__init__",
            "load_private_key",
        ),  # its own throwaway key, in its own directory
        (
            "attacks/runner.py",
            "Stranger.resign",
            "sign",
        ),  # re-signing an edited bundle, as a hostile author can
        ("attacks/runner.py", "Stranger.claim_to_be", "sign"),  # signing a claim to be somebody else
    }


def test_the_attack_labs_stranger_never_holds_the_operators_key(tmp_path: Path) -> None:
    from toolforge.attacks.runner import Stranger
    from toolforge.provenance import keys

    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    operator = keys.init_keys(operator_home)
    lab_root = tmp_path / "lab"
    lab_root.mkdir()
    stranger = Stranger(lab_root, "stranger", [])
    try:
        assert stranger.home.is_relative_to(lab_root) and not stranger.home.is_relative_to(operator_home)
        assert stranger.fingerprint != operator.fingerprint
        assert stranger.raw_public != keys.own_public_key(operator_home).raw
        # It reads its private key from its own directory only: the operator's key file is not in its home.
        assert not (stranger.home / "keys").samefile(operator_home / "keys")
    finally:
        stranger.close()


def test_a_report_that_cannot_be_built_blocks_the_export_with_the_command_that_explains_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolforge.report import blast_radius

    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("secret detail that must not be echoed")

    monkeypatch.setattr(blast_radius, "generate", broken)
    with Registry(home) as registry:
        record = verified(settings, registry)
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert caught.value.code == "report_unavailable" and "toolforge report csv_sum@1.0.0" in str(caught.value)
    assert "secret detail" not in str(caught.value) and not (tmp_path / "x.tfbundle").exists()


def test_a_report_that_is_too_large_blocks_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolforge.report import blast_radius

    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    monkeypatch.setattr(
        blast_radius, "generate", lambda *a, **k: {"big": "x" * (bundle.MEMBER_LIMITS["report.json"] + 1)}
    )
    with Registry(home) as registry:
        record = verified(settings, registry)
        with pytest.raises(ProvenanceError) as caught:
            export_to(settings, registry, record, tmp_path / "x.tfbundle")
    assert caught.value.code == "report_unavailable"


def test_a_package_that_cannot_be_rebuilt_into_an_archive_is_refused(
    tmp_path: Path, origin, receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolforge.errors import ValidationError

    _, _, path = origin

    class Unbuildable:
        @property
        def code_sha(self) -> str:
            raise ValidationError("not buildable")

    monkeypatch.setattr(bundle, "package_from_blob", lambda *_a: Unbuildable())
    fails(path, receiver[0], "package_invalid")
