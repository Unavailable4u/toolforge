"""Versions, blobs attached to versions, gate results, approvals and call statistics."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from toolforge.errors import RegistryError, ValidationError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord

Make = Callable[..., ToolPackage]
Activate = Callable[[VersionRecord], VersionRecord]


# --- Versions --------------------------------------------------------------------------------------------------------


def test_a_new_version_is_submitted_and_holds_metadata_not_source(
    registry: Registry, make_package: Make
) -> None:
    package = make_package(
        "pdf_text", "1.2.0", description="Extract the plain text of a PDF document.", parent_version="1.1.0"
    )
    record = registry.add_version(package, created_by="agent")
    assert (record.name, record.version, record.status) == ("pdf_text", "1.2.0", "submitted")
    assert record.code_sha == package.code_sha and record.blob_sha is None
    assert record.parent_version == "1.1.0" and record.created_by == "agent"
    assert record.manifest["description"].startswith("Extract the plain text")
    stored = registry.db.query_one("SELECT * FROM versions WHERE id = ?", (record.id,))
    assert "def run" not in " ".join(
        str(value) for value in tuple(stored)
    )  # the tool's source is not in the table
    assert registry.get_version(record.id) == record == registry.find_version("pdf_text", "1.2.0")


def test_registering_creates_the_tool_row_once(registry: Registry, make_package: Make) -> None:
    registry.add_version(make_package("word_count", "1.0.0"), created_by="agent")
    registry.add_version(make_package("word_count", "1.1.0"), created_by="agent")
    (tool,) = registry.list_tools()
    assert (tool.name, tool.active_version, tool.versions) == ("word_count", None, 2)


def test_a_version_number_can_be_registered_only_once(registry: Registry, make_package: Make) -> None:
    registry.add_version(make_package("word_count", "1.0.0"), created_by="agent")
    with pytest.raises(RegistryError) as caught:
        registry.add_version(make_package("word_count", "1.0.0"), created_by="agent")
    assert caught.value.code == "version_exists" and "raise the version" in (caught.value.hint or "")
    assert len(registry.list_versions()) == 1


def test_a_failed_registration_leaves_no_tool_row(registry: Registry, make_package: Make) -> None:
    broken = ToolPackage({"name": "x"}, {})  # not a valid manifest
    with pytest.raises(ValidationError):
        registry.add_version(broken, created_by="agent")
    assert registry.list_tools() == [] and registry.list_versions() == []


@pytest.mark.parametrize("creator", ["", "   ", None, "bad\nname"])
def test_created_by_must_be_plain_text(registry: Registry, make_package: Make, creator: object) -> None:
    with pytest.raises(RegistryError):
        registry.add_version(make_package(), created_by=creator)  # type: ignore[arg-type]
    assert registry.list_tools() == []


def test_lists_filter_by_name_and_status(registry: Registry, make_package: Make, activate: Activate) -> None:
    activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    registry.add_version(make_package("word_count", "1.1.0"), created_by="agent")
    registry.add_version(make_package("pdf_text", "1.0.0"), created_by="agent")
    assert [(r.name, r.version) for r in registry.list_versions()] == [
        ("pdf_text", "1.0.0"),
        ("word_count", "1.0.0"),
        ("word_count", "1.1.0"),
    ]
    assert [r.version for r in registry.list_versions(name="word_count")] == ["1.0.0", "1.1.0"]
    assert [r.name for r in registry.list_versions(status="active")] == ["word_count"]
    assert registry.list_versions(name="word_count", status="rejected") == []
    assert registry.get_tool("nope") is None and registry.find_version("word_count", "9.9.9") is None


def test_state_survives_reopening(tmp_path, make_package: Make) -> None:
    with Registry(tmp_path / "home") as first:
        record = first.add_version(make_package(), created_by="agent")
        for status in ("verifying", "verified", "active"):
            first.transition(record.id, status, actor="pipeline")
    with Registry(tmp_path / "home") as second:
        active = second.active_version("word_count")
        assert active is not None and active.id == record.id
        assert [h.name for h in second.search("words")] == ["word_count"]


# --- Blobs attached to versions --------------------------------------------------------------------------------------


def test_attach_a_blob(registry: Registry, make_package: Make) -> None:
    package = make_package()
    record = registry.add_version(package, created_by="agent")
    sha = registry.blobs.put(package.to_tar())
    assert sha == record.code_sha  # no requirements: the frozen blob is the plain archive
    registry.attach_blob(record.id, sha)
    found = registry.get_version(record.id)
    assert found is not None and found.blob_sha == sha
    assert registry.blobs.get(sha) == package.to_tar()
    registry.attach_blob(record.id, sha)  # the same blob again is fine


def test_a_blob_must_exist_and_cannot_change(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    with pytest.raises(RegistryError) as caught:
        registry.attach_blob(record.id, "0" * 64)
    assert caught.value.code == "blob_missing"
    with pytest.raises(RegistryError):
        registry.attach_blob(record.id, "../../etc/passwd")
    registry.attach_blob(record.id, registry.blobs.put(b"first"))
    with pytest.raises(RegistryError) as caught:
        registry.attach_blob(record.id, registry.blobs.put(b"second"))
    assert caught.value.code == "blob_already_set"


def test_a_blob_cannot_be_attached_once_verification_is_over(
    registry: Registry, make_package: Make, activate: Activate
) -> None:
    record = activate(registry.add_version(make_package(), created_by="agent"))
    with pytest.raises(RegistryError) as caught:
        registry.attach_blob(record.id, registry.blobs.put(b"late"))
    assert caught.value.code == "blob_not_attachable"


# --- Gate results ----------------------------------------------------------------------------------------------------


def test_gate_results_are_recorded_in_order(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    registry.record_gate(
        record.id,
        "G1",
        "passed",
        {"checked": 4},
        started_at="2026-01-01T00:00:00.000Z",
        finished_at="2026-01-01T00:00:01.000Z",
    )
    registry.record_gate(record.id, "G3", "failed", {"hints": ["handle empty input"]})
    registry.record_gate(record.id, "G5", "skipped")
    results = registry.gate_results(record.id)
    assert [(g.gate, g.status) for g in results] == [("G1", "passed"), ("G3", "failed"), ("G5", "skipped")]
    assert results[0].detail == {"checked": 4} and results[0].started_at == "2026-01-01T00:00:00.000Z"
    assert results[1].detail == {"hints": ["handle empty input"]} and results[2].detail is None


def test_a_gate_status_must_be_one_of_three(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    for bad in ("error", "PASSED", "", None):
        with pytest.raises(RegistryError):
            registry.record_gate(record.id, "G1", bad)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="CHECK"):  # and the table refuses it even if the code did not
        with registry.db.transaction() as conn:
            conn.execute(
                "INSERT INTO gate_results VALUES (NULL, ?, 'G1', 'maybe', '{}', 'x', 'x')", (record.id,)
            )
    assert registry.gate_results(record.id) == []


def test_gate_detail_must_be_json_and_is_bounded(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    for bad in ({"set": {1, 2}}, {"nan": float("nan")}, {"bytes": b"x"}):
        with pytest.raises(RegistryError):
            registry.record_gate(record.id, "G1", "failed", bad)
    registry.record_gate(record.id, "G3", "failed", {"output": "x" * 200_000})
    (result,) = registry.gate_results(record.id)
    assert (
        result.detail["truncated"] is True and result.detail["size"] > 200_000
    )  # the outcome is kept, the bulk is not


def test_gate_results_need_a_real_version(registry: Registry) -> None:
    with pytest.raises(RegistryError) as caught:
        registry.record_gate(404, "G1", "passed")
    assert caught.value.code == "unknown_version"


# --- Approvals -------------------------------------------------------------------------------------------------------


def test_approval_lifecycle(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(network=["api.example.com"]), created_by="agent")
    approval = registry.add_approval(record.id, {"network": ["api.example.com"], "skipped_gates": ["G5"]})
    assert (
        approval.status == "pending"
        and approval.decided_by is None
        and approval.requested["skipped_gates"] == ["G5"]
    )
    assert [a.id for a in registry.list_approvals(status="pending")] == [approval.id]
    decided = registry.decide_approval(approval.id, "approved", decided_by="operator")
    assert (decided.status, decided.decided_by) == ("approved", "operator") and decided.decided_at is not None
    assert registry.list_approvals(status="pending") == [] and len(registry.list_approvals()) == 1
    with pytest.raises(RegistryError) as caught:
        registry.decide_approval(approval.id, "denied", decided_by="operator")
    assert caught.value.code == "approval_decided"


def test_approval_inputs_are_checked(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    with pytest.raises(RegistryError):
        registry.add_approval(999, {})
    with pytest.raises(RegistryError):
        registry.add_approval(record.id, {"bad": {1}})
    approval = registry.add_approval(record.id, {})
    for bad_status in ("pending", "maybe", ""):
        with pytest.raises(RegistryError):
            registry.decide_approval(approval.id, bad_status, decided_by="operator")
    with pytest.raises(RegistryError):
        registry.decide_approval(approval.id, "approved", decided_by="")
    with pytest.raises(RegistryError):
        registry.decide_approval(777, "approved", decided_by="operator")
    assert registry.get_approval(approval.id).status == "pending"  # type: ignore[union-attr]


# --- Call statistics -------------------------------------------------------------------------------------------------


def _calls(registry: Registry, version_id: int, outcomes: str) -> None:
    for char in outcomes:
        registry.record_call(
            version_id, ok=char == "+", duration_ms=5, error_class=None if char == "+" else "Crash"
        )


def test_fewer_than_five_calls_means_not_measured(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    stats = registry.stats(record.id)
    assert (stats.uses, stats.recent_calls, stats.success_rate) == (0, 0, None)
    _calls(registry, record.id, "++++")
    stats = registry.stats(record.id)
    assert stats.uses == 4 and stats.success_rate is None  # None, never 0 and never 1.0 from four lucky calls
    _calls(registry, record.id, "-")
    assert registry.stats(record.id).success_rate == pytest.approx(0.8)


def test_five_failures_is_a_measured_zero(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    _calls(registry, record.id, "-----")
    assert registry.stats(record.id).success_rate == 0.0


def test_the_rate_covers_only_the_last_fifty_calls(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    _calls(registry, record.id, "-" * 30 + "+" * 50)
    stats = registry.stats(record.id)
    assert (stats.uses, stats.recent_calls, stats.success_rate) == (80, 50, 1.0)
    _calls(registry, record.id, "-" * 10)
    assert registry.stats(record.id).success_rate == pytest.approx(40 / 50)


def test_stats_by_id_name_at_version_and_tool_name(
    registry: Registry, make_package: Make, activate: Activate
) -> None:
    old = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    new = activate(registry.add_version(make_package("word_count", "2.0.0"), created_by="agent"))
    _calls(registry, old.id, "+" * 6)
    _calls(registry, new.id, "+-" * 5)
    assert registry.stats(old.id).uses == 6
    assert registry.stats("word_count@1.0.0").success_rate == 1.0
    assert registry.stats("word_count").uses == 10  # a bare name means its active version
    assert registry.stats("word_count").success_rate == 0.5
    registry.transition(new.id, "quarantined", actor="pipeline", reason="r")
    assert registry.stats("word_count") == registry.stats("word_count").__class__(
        uses=0, recent_calls=0, success_rate=None
    )
    for unknown in ("nope", "nope@1.0.0", "word_count@9.9.9", 12345, None, True):
        with pytest.raises(RegistryError):
            registry.stats(unknown)  # type: ignore[arg-type]


def test_a_call_is_recorded_with_clipped_text(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    registry.record_call(
        record.id, ok=False, duration_ms=12, error_class="Crash\x1b[31m" + "x" * 500, session_id="s-1"
    )
    row = registry.db.query_one("SELECT * FROM calls")
    assert row["ok"] == 0 and row["duration_ms"] == 12 and row["session_id"] == "s-1"
    assert "\x1b" not in row["error_class"] and len(row["error_class"]) <= 80
    assert row["ts"].endswith("Z")


def test_bad_calls_are_refused(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    for bad in (-1, 1.5, "7", True, None):
        with pytest.raises(RegistryError) as caught:
            registry.record_call(record.id, ok=True, duration_ms=bad)  # type: ignore[arg-type]
        assert caught.value.code == "invalid_call"
    with pytest.raises(RegistryError) as caught:
        registry.record_call(999, ok=True, duration_ms=1)
    assert caught.value.code == "unknown_version"
    assert registry.db.query_one("SELECT COUNT(*) FROM calls")[0] == 0


def test_manifest_json_is_canonical(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    row = registry.db.query_one("SELECT manifest_json FROM versions WHERE id = ?", (record.id,))
    text = row["manifest_json"]
    assert json.loads(text) == record.manifest and text == json.dumps(
        record.manifest, sort_keys=True, ensure_ascii=True
    )
