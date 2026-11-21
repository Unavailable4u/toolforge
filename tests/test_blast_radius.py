"""The Blast Radius report: built from real, hash-chained ledger files (written through `ToolforgeLedger`), never from claims."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.report import blast_radius as br

pytestmark = pytest.mark.usefixtures("minilocker")

LABEL = "csv_summary@1.0.0"
SEEDED = {"items": [{"id": "c1", "label": "AWS access key"}, {"id": "c2", "label": "SSH key"}]}
STATS = {
    "peak_memory_bytes": 50 * 1024 * 1024,
    "memory_limit_bytes": 256 * 1024 * 1024,
    "peak_pids": 4,
    "pids_limit": 64,
    "cpu_seconds": 1.2,
    "oom_killed": False,
    "samples": 5,
}
FS = {"rootfs_changes": 0, "rootfs_changed": [], "host_binds": [], "tmpfs": ["/tmp"], "inside": True}
SECRETS = {"checked": ["OPENAI_API_KEY"], "found": [], "found_where": {}, "inside": True}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path)


def sandbox_events(
    *,
    name: str = "sb1",
    network_mode: str = "none",
    decoys: bool = True,
    touched: tuple[str, ...] = (),
    audit: bool = True,
    destroyed: bool = True,
    fs: dict | None = None,
    secrets: dict | None = None,
    stats: dict | None = None,
) -> list[tuple[str, str, dict]]:
    events: list[tuple[str, str, dict]] = [
        ("sandbox", "sandbox.created", {"name": name, "hardening": {"network_mode": network_mode}})
    ]
    if decoys:
        events.append(("sandbox", "canary.seeded", dict(SEEDED)))
    events += [("sandbox", "canary.read", {"item": item, "label": "x"}) for item in touched]
    if audit:
        events.append(
            (
                "sandbox",
                "sandbox.audit",
                {
                    "name": name,
                    "kind": "code",
                    "incomplete": [],
                    "stats": stats or dict(STATS),
                    "fs": fs or dict(FS),
                    "secrets": secrets or dict(SECRETS),
                },
            )
        )
    if destroyed:
        events.append(("sandbox", "sandbox.destroyed", {"name": name}))
    return events


def write_ledger(path: Path, events: list[tuple[str, str, dict]]) -> Path:
    ledger = ToolforgeLedger(path, "t")
    for actor, type, payload in events:
        ledger.append(actor, type, payload)
    return path


def verification_ledger(
    settings: Settings, body: list, *, name: str = "verify-r1.jsonl", build_first: bool = True
) -> Path:
    events = [("forge", "forge.verifying", {})]
    if build_first:  # the build gate's sandbox must not count
        events += [("forge", "forge.gate.started", {"gate": "G2"})]
        events += sandbox_events(name="build", network_mode="bridge", decoys=False, audit=False)
    events += [("forge", "forge.gate.started", {"gate": "G6"})] + body
    return write_ledger(Path(settings.home) / "ledgers" / LABEL / name, events)


def report(settings: Settings, **kw):
    name, version = LABEL.split("@")
    return br.generate(settings, name, version, **kw)


def test_a_clean_contained_run_says_no_violation_seen_and_no_route(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    doc = report(settings)
    assert doc["verdict"]["status"] in {"no_violation_seen", "incomplete"}
    assert doc["ledger_verified"] is True
    assert doc["dimensions"]["network"]["status"] == "no_route"
    assert doc["dimensions"]["secrets"]["decoys_planted"] == 2
    assert doc["dimensions"]["secrets"]["decoys_read"] == []
    assert doc["dimensions"]["filesystem"]["status"] == "measured"
    assert doc["dimensions"]["compute"]["status"] == "measured"
    assert "evidence, not proof" in doc["footer"]
    assert set(doc["not_applicable"]) == {"policy", "time", "browser", "artifacts"}


def test_only_the_containment_run_is_measured_not_the_build_gate(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    doc = report(settings)
    assert doc["sources"]["verification"]["events_measured"] < doc["sources"]["verification"]["events"]
    # the build sandbox had a bridge network; if it had counted, network could not be `no_route`
    assert doc["dimensions"]["network"]["status"] == "no_route"
    assert doc["dimensions"]["network"]["sandboxes"] == 1


def test_a_touched_decoy_is_a_violation(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events(touched=("c1",)))
    doc = report(settings)
    assert doc["verdict"]["status"] == "violation_seen"
    assert any("c1" in v for v in doc["verdict"]["violations"])
    assert doc["dimensions"]["secrets"]["decoys_read"] == ["c1"]


def test_root_filesystem_changes_and_a_real_secret_are_violations(settings: Settings) -> None:
    fs = {**FS, "rootfs_changes": 1, "rootfs_changed": [{"path": "/etc/x", "kind": "C"}]}
    secrets = {**SECRETS, "found": ["OPENAI_API_KEY"], "found_where": {"OPENAI_API_KEY": ["env"]}}
    verification_ledger(settings, sandbox_events(fs=fs, secrets=secrets))
    doc = report(settings)
    reasons = " ".join(doc["verdict"]["violations"])
    assert doc["verdict"]["status"] == "violation_seen"
    assert "root filesystem changed" in reasons and "real secret" in reasons


def test_no_audit_is_not_measured_never_zero(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events(audit=False))
    doc = report(settings)
    for name in ("filesystem", "compute"):
        assert doc["dimensions"][name]["status"] == "not_measured"
        assert "rootfs_changes" not in doc["dimensions"][name] and "peak_pids" not in doc["dimensions"][name]
    assert doc["verdict"]["status"] in {"not_measured", "incomplete"}
    assert any("filesystem was not measured" in g for g in doc["verdict"]["not_measured"])


def test_no_decoy_planted_says_so_instead_of_an_empty_list(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events(decoys=False))
    secrets = report(settings)["dimensions"]["secrets"]
    assert secrets["decoys_read"] == "not measured"
    assert "no decoy was planted" in secrets["decoys_note"]


def test_a_sandbox_that_was_not_destroyed_is_not_finished(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events(destroyed=False))
    doc = report(settings)
    assert doc["dimensions"]["filesystem"]["status"] == "in_progress"
    assert any("not finished" in g or "had not finished" in g for g in doc["verdict"]["not_measured"])
    assert doc["verdict"]["status"] == "incomplete"


def test_a_network_that_was_not_closed_is_not_no_route(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events(network_mode="bridge"))
    assert report(settings)["dimensions"]["network"]["status"] == "not_measured"


def test_egress_events_are_reported_as_attempts(settings: Settings) -> None:
    body = sandbox_events()
    body.insert(1, ("sandbox", "egress.blocked", {"host": "evil.example"}))
    verification_ledger(settings, body)
    doc = report(settings)
    assert doc["dimensions"]["network"]["attempts"] == 1
    assert doc["dimensions"]["network"]["hosts"] == ["evil.example"]
    assert doc["verdict"]["status"] == "violation_seen"


def test_a_tampered_ledger_is_ledger_broken(settings: Settings) -> None:
    path = verification_ledger(settings, sandbox_events())
    lines = path.read_text().splitlines()
    forged = json.loads(lines[-2])
    forged["payload"]["name"] = "forged"
    lines[-2] = json.dumps(forged)
    path.write_text("\n".join(lines) + "\n")
    doc = report(settings)
    assert doc["ledger_verified"] is False
    assert doc["verdict"]["status"] == "ledger_broken"


def test_an_unreadable_line_is_ledger_broken_not_skipped(settings: Settings) -> None:
    path = verification_ledger(settings, sandbox_events())
    lines = path.read_text().splitlines()
    lines.insert(3, "{not json")
    path.write_text("\n".join(lines) + "\n")
    assert report(settings)["verdict"]["status"] == "ledger_broken"


def test_no_ledgers_at_all_is_not_measured(settings: Settings) -> None:
    doc = report(settings)
    assert doc["verdict"]["status"] == "not_measured"
    assert all(d["status"] == "not_measured" for d in doc["dimensions"].values())
    assert doc["ledger_verified"] is False


def test_hostile_text_from_the_tool_is_cleaned(settings: Settings) -> None:
    fs = {
        **FS,
        "rootfs_changes": 1,
        "rootfs_changed": [{"path": "/etc/\x1b[31mevil‮" + "A" * 900, "kind": "A"}],
    }
    verification_ledger(settings, sandbox_events(fs=fs))
    text = json.dumps(report(settings))
    assert "\\u001b" not in text and "‮" not in text and "\\u202e" not in text
    assert "A" * 400 not in text


def test_the_report_is_stored_and_loaded_back(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    doc = report(settings)
    path = br.report_path(settings, LABEL)
    assert path.is_file() and path.parent.name == LABEL
    loaded, problem = br.load_stored(settings, LABEL)
    assert problem is None and loaded == doc
    assert not list(path.parent.glob(".blast-*"))  # the temporary file is gone


def test_store_false_writes_nothing(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    report(settings, store=False)
    assert not br.report_path(settings, LABEL).exists()


def test_loading_handles_a_missing_garbled_or_foreign_file(settings: Settings) -> None:
    assert br.load_stored(settings, LABEL) == (None, None)
    path = br.report_path(settings, LABEL)
    path.parent.mkdir(parents=True)
    path.write_text("{nope")
    assert br.load_stored(settings, LABEL)[0] is None and br.load_stored(settings, LABEL)[1]
    path.write_text(json.dumps({"schema": 99, "dimensions": {}}))
    assert "unknown format" in br.load_stored(settings, LABEL)[1]
    path.write_text(json.dumps({"schema": 1, "dimensions": {}}) + " " * (br.MAX_STORED_BYTES + 1))
    assert "larger" in br.load_stored(settings, LABEL)[1]


def test_real_calls_are_sampled_and_a_touched_decoy_there_is_a_violation(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    folder = Path(settings.home) / "ledgers" / LABEL
    write_ledger(folder / "call-1.jsonl", sandbox_events(name="c1"))
    write_ledger(folder / "call-2.jsonl", sandbox_events(name="c2", touched=("c2",)))
    write_ledger(folder / "operator.jsonl", [("operator", "forge.promoted", {})])  # not a call ledger
    doc = report(settings)
    sample = doc["runtime_sample"]
    assert sample["calls_sampled"] == 2 and sample["calls_found"] == 2
    assert sample["calls_with_decoys_planted"] == 2 and sample["decoy_watch"] == "complete"
    assert sample["decoys_touched"] == ["c2"] and sample["calls_with_decoy_events"] == 1
    assert doc["verdict"]["status"] == "violation_seen"


def test_calls_without_decoys_are_a_partial_watch_not_a_clean_one(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    folder = Path(settings.home) / "ledgers" / LABEL
    write_ledger(folder / "call-1.jsonl", sandbox_events(name="c1", decoys=False))
    doc = report(settings)
    assert doc["runtime_sample"]["decoy_watch"] == "not_measured"
    assert doc["verdict"]["status"] == "incomplete"


def test_no_real_call_yet_is_a_stated_gap(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    doc = report(settings)
    assert doc["runtime_sample"]["status"] == "not_measured"
    assert any("no real call" in g for g in doc["verdict"]["not_measured"])


def test_only_the_newest_calls_are_sampled(settings: Settings) -> None:
    verification_ledger(settings, sandbox_events())
    folder = Path(settings.home) / "ledgers" / LABEL
    for i in range(br.MAX_SAMPLED_CALLS + 3):
        write_ledger(folder / f"call-{i:03}.jsonl", sandbox_events(name=f"c{i}"))
    sample = report(settings)["runtime_sample"]
    assert (
        sample["calls_sampled"] == br.MAX_SAMPLED_CALLS and sample["calls_found"] == br.MAX_SAMPLED_CALLS + 3
    )


def test_slice_starts_at_the_last_g6_start_and_keeps_everything_for_a_dry_run() -> None:
    events = [{"type": "a"}, {"type": "forge.gate.started", "payload": {"gate": "G6"}}, {"type": "b"}]
    assert [e["type"] for e in br.slice_from_gate(events)] == ["forge.gate.started", "b"]
    assert br.slice_from_gate([{"type": "a"}]) == [{"type": "a"}]


def test_summarize_keeps_small_maps_such_as_real_secrets_found(settings: Settings) -> None:
    secrets = {**SECRETS, "found": ["K"], "found_where": {"K": ["env", "file"]}}
    verification_ledger(settings, sandbox_events(secrets=secrets))
    summary = br.summarize(report(settings))
    assert summary["dimensions"]["secrets"]["real_secrets_found"] == {"K": ["env", "file"]}
    assert summary["status"] == "violation_seen"


def test_the_missing_summary_says_not_measured() -> None:
    summary = br.missing_summary("the stored report could not be read")
    assert summary["status"] == "not_measured" and summary["ledger_verified"] is False
    assert all(d == {"status": "not_measured"} for d in summary["dimensions"].values())


def test_text_and_html_renderings(settings: Settings) -> None:
    fs = {**FS, "rootfs_changes": 1, "rootfs_changed": [{"path": "/x<script>alert(1)</script>", "kind": "A"}]}
    verification_ledger(settings, sandbox_events(fs=fs))
    doc = report(settings)
    text = br.render_text(doc)
    assert (
        text.startswith(f"Blast Radius: {LABEL}")
        and "violation_seen" in text
        and "evidence, not proof" in text
    )
    page = br.render_html(doc)
    assert page.lstrip().lower().startswith("<!doctype html") or "<html" in page.lower()
    assert "<script>alert(1)</script>" not in page
    assert LABEL in page and "n/a steps" not in page


def test_nothing_in_the_report_module_imports_minilocker_by_name_at_module_level() -> None:
    source = Path(br.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("minilocker") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("minilocker")


# --- Found on a real Docker daemon -------------------------------------------------------------------------------------------------------


def test_the_workspace_mount_point_listed_as_modified_is_not_a_violation(settings: Settings) -> None:
    fs = {
        **FS,
        "rootfs_changes": 1,
        "rootfs_changed": [{"path": "/workspace", "kind": "modified"}],
        "tmpfs": ["/tmp", "/workspace"],
    }
    verification_ledger(settings, sandbox_events(fs=fs))
    doc = report(settings)
    assert doc["dimensions"]["filesystem"]["rootfs_changes"] == 0
    assert doc["dimensions"]["filesystem"]["mount_points_ignored"] == 1
    assert doc["verdict"]["status"] != "violation_seen", doc["verdict"]


def test_a_real_change_beside_a_mount_point_is_still_a_violation(settings: Settings) -> None:
    fs = {
        **FS,
        "rootfs_changes": 2,
        "rootfs_changed": [{"path": "/workspace", "kind": "modified"}, {"path": "/etc/x", "kind": "added"}],
        "tmpfs": ["/workspace"],
    }
    verification_ledger(settings, sandbox_events(fs=fs))
    doc = report(settings)
    assert doc["dimensions"]["filesystem"]["rootfs_changes"] == 1
    assert any("1 changes" in v for v in doc["verdict"]["violations"])


def test_a_verification_ledger_with_no_g6_start_is_not_measured_not_judged_on_other_gates(
    settings: Settings,
) -> None:
    # a version verified before G6 existed: build and test sandboxes only, one of them with a changed mount point
    fs = {**FS, "rootfs_changes": 2, "rootfs_changed": [{"path": "/etc/x", "kind": "added"}]}
    events = [
        ("forge", "forge.verifying", {}),
        ("forge", "forge.gate.started", {"gate": "G3"}),
    ] + sandbox_events(fs=fs, decoys=False)
    write_ledger(Path(settings.home) / "ledgers" / LABEL / "verify-old.jsonl", events)
    doc = report(settings)
    assert doc["verdict"]["status"] == "not_measured" and doc["verdict"]["violations"] == []
    assert any("did not run" in g for g in doc["verdict"]["not_measured"])
    assert all(d["status"] == "not_measured" for d in doc["dimensions"].values() if "status" in d)
    assert doc["sources"]["verification"]["events_measured"] == 0
    assert doc["sources"]["verification"]["verified"] is True  # the chain itself is still checked
    assert "gate G6 did not run" in doc["sources"]["verification"]["scope"]
