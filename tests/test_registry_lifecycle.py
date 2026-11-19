"""The lifecycle: allowed and illegal transitions, who may cause them, and the atomic promotion of a version."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from toolforge.errors import IllegalTransition, RegistryError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord, lifecycle, search
from toolforge.registry.lifecycle import EDGES, STATUSES, TRANSITIONS

ROOT = Path(__file__).resolve().parent.parent

Make = Callable[..., ToolPackage]


def _walk(registry: Registry, record: VersionRecord, *path: str) -> None:
    """Move a version along `path`, acting as the operator where the state machine demands it."""
    for status in path:
        edge = EDGES[(registry.get_version(record.id).status, status)]  # type: ignore[union-attr]
        registry.transition(
            record.id,
            status,
            actor=lifecycle.OPERATOR if edge.operator_only else "pipeline",
            reason="because" if edge.reason_required else None,
        )


def _status(registry: Registry, record: VersionRecord) -> str:
    found = registry.get_version(record.id)
    assert found is not None
    return found.status


# --- The state machine itself ----------------------------------------------------------------------------------------


def test_the_guide_lists_the_same_statuses_and_arrows() -> None:
    """`docs/guide/02-architecture.md` shows the lifecycle as a diagram; the code must agree with it."""
    diagram = (ROOT / "docs" / "guide" / "02-architecture.md").read_text(encoding="utf-8")
    block = re.search(r"stateDiagram-v2\n(.*?)```", diagram, re.DOTALL)
    assert block, "the stateDiagram-v2 block moved; update this test"
    arrows = set(re.findall(r"^\s*(\w+) --> (\w+)", block.group(1), re.MULTILINE)) - {("[*]", "submitted")}
    assert arrows == set(EDGES), (
        "docs/guide/02-architecture.md and registry/lifecycle.py disagree about the lifecycle"
    )
    assert set(STATUSES) == {s for edge in EDGES for s in edge} | {"submitted"}


def test_spec_examples_are_allowed() -> None:
    assert "pending_approval" in TRANSITIONS["verified_basic"]
    assert "active" in TRANSITIONS["deprecated"]
    assert "active" in TRANSITIONS["quarantined"]


def test_rejected_is_final_and_deprecated_only_comes_from_active() -> None:
    assert TRANSITIONS["rejected"] == ()
    assert [frm for (frm, to) in EDGES if to == "deprecated"] == [
        "active"
    ]  # so every deprecated version was active once


@pytest.mark.parametrize(("frm", "to"), list(itertools.product(STATUSES, STATUSES)))
def test_every_pair_of_statuses(registry: Registry, make_package: Make, frm: str, to: str) -> None:
    """Exactly the listed edges are allowed; every other pair raises and changes nothing."""
    record = registry.add_version(make_package(), created_by="agent")
    path = {
        "submitted": [],
        "verifying": ["verifying"],
        "rejected": ["verifying", "rejected"],
        "verified_basic": ["verifying", "verified_basic"],
        "verified": ["verifying", "verified"],
        "pending_approval": ["verifying", "verified", "pending_approval"],
        "active": ["verifying", "verified", "active"],
        "deprecated": ["verifying", "verified", "active", "deprecated"],
        "quarantined": ["verifying", "verified", "active", "quarantined"],
    }[frm]
    _walk(registry, record, *path)
    assert _status(registry, record) == frm
    if (frm, to) in EDGES:
        edge = EDGES[(frm, to)]
        event = registry.transition(
            record.id, to, actor="operator", reason="a reason" if edge.reason_required else None
        )
        assert _status(registry, record) == to and event["type"] == edge.event
    else:
        with pytest.raises(IllegalTransition):
            registry.transition(record.id, to, actor="operator", reason="a reason")
        assert _status(registry, record) == frm


def test_an_unknown_status_or_version_is_refused(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    with pytest.raises(IllegalTransition):
        registry.transition(record.id, "published", actor="pipeline")
    with pytest.raises(RegistryError) as caught:
        registry.transition(9999, "verifying", actor="pipeline")
    assert caught.value.code == "unknown_version"


def test_an_illegal_move_explains_itself(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    with pytest.raises(IllegalTransition) as caught:
        registry.transition(record.id, "active", actor="operator", reason="skip the gates")
    message = str(caught.value)
    assert "word_count@1.0.0" in message and "submitted" in message and "verifying" in message
    assert caught.value.code == "illegal_transition"


@pytest.mark.parametrize("actor", [None, "", "  ", "bad\x00actor", 5])
def test_an_actor_is_required(registry: Registry, make_package: Make, actor: object) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    with pytest.raises(RegistryError) as caught:
        registry.transition(record.id, "verifying", actor=actor)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_actor"


# --- Who may do what -------------------------------------------------------------------------------------------------


def test_promoting_a_verified_basic_version_needs_the_operator_and_a_reason(
    registry: Registry, make_package: Make
) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    _walk(registry, record, "verifying", "verified_basic")
    with pytest.raises(IllegalTransition, match="only the operator"):
        registry.transition(record.id, "active", actor="pipeline", reason="why not")
    with pytest.raises(IllegalTransition, match="needs a reason"):
        registry.transition(record.id, "active", actor="operator")
    with pytest.raises(IllegalTransition, match="needs a reason"):
        registry.transition(record.id, "active", actor="operator", reason="   ")
    assert _status(registry, record) == "verified_basic"
    event = registry.transition(record.id, "active", actor="operator", reason="reviewed by hand")
    assert event["type"] == "forge.promoted" and event["reason"] == "reviewed by hand"
    assert _status(registry, record) == "active"


@pytest.mark.parametrize(
    ("network", "secrets"),
    [(["api.example.com"], []), ([], ["weather_key"]), (["api.example.com"], ["weather_key"])],
)
@pytest.mark.parametrize("start", ["verified_basic", "verified"])
def test_promotion_is_refused_for_network_or_secrets(
    registry: Registry, make_package: Make, start: str, network: list[str], secrets: list[str]
) -> None:
    record = registry.add_version(make_package(network=network, secrets=secrets), created_by="agent")
    _walk(registry, record, "verifying", start)
    with pytest.raises(IllegalTransition, match="without approval") as caught:
        registry.transition(record.id, "active", actor="operator", reason="trust me")
    assert "pending_approval" in (caught.value.hint or "")
    assert _status(registry, record) == start
    assert registry.active_version("word_count") is None
    # the way through is approval
    _walk(registry, record, "pending_approval", "active")
    assert _status(registry, record) == "active"


def test_only_the_operator_can_approve(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(network=["api.example.com"]), created_by="agent")
    _walk(registry, record, "verifying", "verified", "pending_approval")
    with pytest.raises(IllegalTransition, match="only the operator"):
        registry.transition(record.id, "active", actor="pipeline")
    event = registry.transition(record.id, "active", actor="operator")
    assert event["type"] == "forge.approval.granted"


def test_denial_or_timeout_rejects(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(network=["api.example.com"]), created_by="agent")
    _walk(registry, record, "verifying", "verified_basic", "pending_approval")
    event = registry.transition(record.id, "rejected", actor="timeout", reason="no answer in 900 s")
    assert event["type"] == "forge.approval.denied" and _status(registry, record) == "rejected"


@pytest.mark.parametrize("via", ["quarantined", "deprecated"])
def test_release_and_rollback_need_the_operator_and_a_reason(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord], via: str
) -> None:
    record = activate(registry.add_version(make_package(), created_by="agent"))
    registry.transition(record.id, via, actor="pipeline", reason="runtime violation")
    with pytest.raises(IllegalTransition, match="only the operator"):
        registry.transition(record.id, "active", actor="pipeline", reason="oops")
    with pytest.raises(IllegalTransition, match="needs a reason"):
        registry.transition(record.id, "active", actor="operator")
    event = registry.transition(record.id, "active", actor="operator", reason="false alarm")
    assert event["type"] == ("forge.released" if via == "quarantined" else "forge.rolled_back")


def test_event_shape_and_reason_cleaning(registry: Registry, make_package: Make) -> None:
    record = registry.add_version(make_package(), created_by="agent")
    event = registry.transition(
        record.id, "verifying", actor="  pipeline ", reason="line one\nline two\x00" + "x" * 900
    )
    assert set(event) == {
        "type",
        "tool",
        "version",
        "version_id",
        "from",
        "to",
        "actor",
        "code_sha",
        "ts",
        "reason",
        "related",
    }
    assert event["type"] == "forge.verifying" and event["actor"] == "pipeline"
    assert event["tool"] == "word_count" and event["version"] == "1.0.0" and event["version_id"] == record.id
    assert (event["from"], event["to"]) == ("submitted", "verifying") and event["code_sha"] == record.code_sha
    assert event["related"] == []
    assert (
        "\n" not in event["reason"]
        and "\x00" not in event["reason"]
        and len(event["reason"]) <= lifecycle.MAX_REASON_CHARS
    )
    assert "reason" not in registry.transition(record.id, "rejected", actor="pipeline")


def test_updated_at_moves_with_a_transition(tmp_path: Path, make_package: Make) -> None:
    ticks = iter(f"2026-11-19T00:00:{n:02d}.000Z" for n in range(60))
    with Registry(tmp_path / "home", clock=lambda: next(ticks)) as registry:
        record = registry.add_version(make_package(), created_by="agent")
        registry.transition(record.id, "verifying", actor="pipeline")
        after = registry.get_version(record.id)
        assert (
            after is not None
            and after.created_at == record.created_at
            and after.updated_at > record.updated_at
        )


# --- Promotion, deprecation, quarantine ------------------------------------------------------------------------------


def test_promotion_sets_the_active_version_and_the_search_index(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    record = activate(registry.add_version(make_package(), created_by="agent"))
    active = registry.active_version("word_count")
    assert active is not None and active.id == record.id
    assert [hit.name for hit in registry.search("words")] == ["word_count"]


def test_promotion_deprecates_the_previous_active_version(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    first = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    second = registry.add_version(
        make_package("word_count", "1.1.0", parent_version="1.0.0"), created_by="agent"
    )
    _walk(registry, second, "verifying", "verified")
    event = registry.transition(second.id, "active", actor="pipeline")
    assert _status(registry, first) == "deprecated" and _status(registry, second) == "active"
    active = registry.active_version("word_count")
    assert active is not None and active.version == "1.1.0"
    assert [hit.version for hit in registry.search("words")] == ["1.1.0"]
    (related,) = event["related"]
    assert (
        related["type"] == "forge.deprecated"
        and related["version"] == "1.0.0"
        and related["version_id"] == first.id
    )
    assert (related["from"], related["to"]) == ("active", "deprecated")


def test_a_failure_during_promotion_changes_nothing(
    registry: Registry,
    make_package: Make,
    activate: Callable[[VersionRecord], VersionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last step of promotion indexes the new version. If it fails, the old version must still be active, found by search, and the
    new version must still be `verified`: nothing is half done."""
    first = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    second = registry.add_version(
        make_package("word_count", "2.0.0", parent_version="1.0.0"), created_by="agent"
    )
    _walk(registry, second, "verifying", "verified")
    before = (registry.list_versions(), registry.list_tools(), [h.version for h in registry.search("words")])

    def explode(*_: object) -> None:
        raise RuntimeError("index write failed")

    monkeypatch.setattr(search, "index_version", explode)
    with pytest.raises(RuntimeError, match="index write failed"):
        registry.transition(second.id, "active", actor="pipeline")
    monkeypatch.undo()

    assert (
        registry.list_versions(),
        registry.list_tools(),
        [h.version for h in registry.search("words")],
    ) == before
    assert _status(registry, first) == "active" and _status(registry, second) == "verified"
    current = registry.active_version("word_count")
    assert current is not None and current.version == "1.0.0"
    # and the registry still works afterwards
    registry.transition(second.id, "active", actor="pipeline")
    assert [h.version for h in registry.search("words")] == ["2.0.0"]


def test_a_failure_while_removing_the_old_index_row_also_changes_nothing(
    registry: Registry,
    make_package: Make,
    activate: Callable[[VersionRecord], VersionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    second = registry.add_version(make_package("word_count", "2.0.0"), created_by="agent")
    _walk(registry, second, "verifying", "verified")
    monkeypatch.setattr(search, "remove_version", lambda *_: (_ for _ in ()).throw(RuntimeError("no")))
    with pytest.raises(RuntimeError):
        registry.transition(second.id, "active", actor="pipeline")
    monkeypatch.undo()
    assert _status(registry, first) == "active" and _status(registry, second) == "verified"


def test_quarantine_clears_the_active_version_and_the_search_row(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    record = activate(registry.add_version(make_package(), created_by="agent"))
    event = registry.transition(record.id, "quarantined", actor="pipeline", reason="decoy touched")
    assert event["type"] == "forge.quarantined"
    assert registry.active_version("word_count") is None
    tool = registry.get_tool("word_count")
    assert tool is not None and tool.active_version is None
    assert registry.search("words") == []


def test_release_brings_a_quarantined_version_back(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    record = activate(registry.add_version(make_package(), created_by="agent"))
    registry.transition(record.id, "quarantined", actor="pipeline", reason="failure rate")
    registry.transition(record.id, "active", actor="operator", reason="fixed upstream")
    active = registry.active_version("word_count")
    assert active is not None and active.id == record.id
    assert len(registry.search("words")) == 1


def test_rollback_is_the_mirror_image_of_promotion(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    first = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    second = registry.add_version(
        make_package("word_count", "2.0.0", parent_version="1.0.0"), created_by="agent"
    )
    _walk(registry, second, "verifying", "verified", "active")
    assert _status(registry, first) == "deprecated"
    event = registry.transition(first.id, "active", actor="operator", reason="2.0.0 breaks my pipeline")
    assert event["type"] == "forge.rolled_back"
    assert _status(registry, first) == "active" and _status(registry, second) == "deprecated"
    active = registry.active_version("word_count")
    assert active is not None and active.version == "1.0.0"
    assert [h.version for h in registry.search("words")] == ["1.0.0"]
    assert [r["version"] for r in event["related"]] == ["2.0.0"]


def test_releasing_a_quarantined_version_displaces_one_promoted_in_the_meantime(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    first = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    registry.transition(first.id, "quarantined", actor="pipeline", reason="violation")
    second = activate(registry.add_version(make_package("word_count", "1.1.0"), created_by="agent"))
    registry.transition(first.id, "active", actor="operator", reason="cleared")
    assert _status(registry, first) == "active" and _status(registry, second) == "deprecated"
    assert len(registry.list_versions(name="word_count", status="active")) == 1


def test_other_tools_are_untouched_by_a_promotion(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    other = activate(
        registry.add_version(
            make_package("pdf_text", "1.0.0", description="Extract the text from a PDF file."),
            created_by="agent",
        )
    )
    activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    assert _status(registry, other) == "active"
    assert {t.name: t.active_version for t in registry.list_tools()} == {
        "pdf_text": "1.0.0",
        "word_count": "1.0.0",
    }


def test_rows_are_never_deleted(
    registry: Registry, make_package: Make, activate: Callable[[VersionRecord], VersionRecord]
) -> None:
    first = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    activate(registry.add_version(make_package("word_count", "2.0.0"), created_by="agent"))
    assert [r.status for r in registry.list_versions(name="word_count")] == ["deprecated", "active"]
    assert registry.get_version(first.id) is not None
