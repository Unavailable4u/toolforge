"""Stored attack and benchmark results: shown when there, a gap when not, and never run from here."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dashboard_helpers import make_dashboard

from toolforge.dashboard import results
from toolforge.registry import Registry


def test_no_results_is_said_as_a_gap(tmp_path) -> None:
    for kind in results.KINDS:
        out = results.stored_results(tmp_path / "runs", kind)
        assert (
            out["available"] is False
            and out["latest"] is None
            and out["files"] == []
            and "no stored" in out["message"]
        )


def test_the_newest_attack_file_is_shown_and_the_others_are_listed(tmp_path) -> None:
    folder = tmp_path / "runs" / "attacks"
    folder.mkdir(parents=True)
    old, new = folder / "old.json", folder / "new.json"
    old.write_text(json.dumps({"contained": 1, "total": 5}))
    new.write_text(json.dumps({"contained": 5, "total": 5, "per_attack": {"a1": {"verdict": "contained"}}}))
    os.utime(old, (1, 1))
    out = results.stored_results(tmp_path / "runs", "attacks")
    assert out["available"] is True and out["latest"]["name"] == "new.json"
    assert out["latest"]["document"]["per_attack"]["a1"]["verdict"] == "contained"
    assert [f["name"] for f in out["files"]] == ["new.json", "old.json"]
    assert (
        results.stored_results(tmp_path / "runs", "attacks", "old.json")["latest"]["document"]["total"] == 5
    )


def test_a_real_attack_run_file_is_read_in_the_shape_the_lab_writes(tmp_path) -> None:
    from toolforge.attacks import runner
    from toolforge.attacks.catalog import BREACHED, CONTAINED, INCONCLUSIVE, SKIPPED, CheckResult

    outcomes = [
        runner.AttackResult(
            "a1", "Held", CONTAINED, "the defence held", [CheckResult("blocked", "pass", "yes")], 1.2
        ),
        runner.AttackResult(
            "a2", "Broke", BREACHED, "it got out", [CheckResult("blocked", "fail", "no")], 0.4
        ),
        runner.AttackResult(
            "a3", "Unsure", INCONCLUSIVE, "no figures", [CheckResult("blocked", "unproven", "?")]
        ),
        runner.AttackResult("a4", "Skipped", SKIPPED, "no docker", []),
    ]
    written = runner.write_json(outcomes, tmp_path / "runs" / "attacks", docker="docker is not installed")
    out = results.stored_results(tmp_path / "runs", "attacks")
    assert out["latest"]["name"] == written.name and written.name.startswith("attacks-")
    document = out["latest"]["document"]
    assert document["summary"] == {"contained": 1, "breached": 1, "inconclusive": 1, "skipped": 1}
    assert [r["verdict"] for r in document["results"]] == [CONTAINED, BREACHED, INCONCLUSIVE, SKIPPED]
    assert document["results"][0]["checks"][0] == {
        "label": "blocked",
        "status": "pass",
        "detail": "yes",
        "scope": "attack",
    }
    assert document["docker"] == "docker is not installed" and "evidence, not proof" in document["footer"]


def test_the_attack_page_colours_only_contained_green() -> None:
    from importlib import resources

    js = (resources.files("toolforge.dashboard") / "static" / "app.js").read_text(encoding="utf-8")
    assert "contained: 'ok', breached: 'bad', inconclusive: 'warn', skipped: 'warn'" in js
    assert "doc.results.every((r) => r && typeof r.verdict === 'string')" in js


def test_a_benchmark_result_is_the_summary_of_a_run_directory(tmp_path) -> None:
    run = tmp_path / "runs" / "bench" / "20261126-101500"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"configs": {"baseline": {"solved": 3}}}))
    (run / "ignored.json").write_text("{}")
    out = results.stored_results(tmp_path / "runs", "bench")
    assert (
        out["latest"]["name"] == "20261126-101500/summary.json"
        and out["latest"]["document"]["configs"]["baseline"]["solved"] == 3
    )


def test_a_name_is_looked_up_in_the_listing_and_never_joined_to_a_path(tmp_path) -> None:
    folder = tmp_path / "runs" / "attacks"
    folder.mkdir(parents=True)
    (folder / "a.json").write_text("{}")
    (tmp_path / "secret.json").write_text('{"leak": true}')
    for name in ("../../secret.json", "/etc/passwd", "..%2f..%2fsecret.json", "a.json/../../../secret.json"):
        out = results.stored_results(tmp_path / "runs", "attacks", name)
        assert out["available"] is False and out["latest"] is None and "leak" not in json.dumps(out)


def test_bad_oversized_and_linked_files_are_not_read(tmp_path) -> None:
    folder = tmp_path / "runs" / "attacks"
    folder.mkdir(parents=True)
    (folder / "bad.json").write_text("{not json")
    assert "could not be read" in results.stored_results(tmp_path / "runs", "attacks")["message"]
    (folder / "bad.json").unlink()
    big = folder / "big.json"
    big.write_text(" " * (results.MAX_RESULT_BYTES + 1))
    assert "larger than 2 MB" in results.stored_results(tmp_path / "runs", "attacks")["message"]
    big.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{"leak": true}')
    (folder / "link.json").symlink_to(outside)
    assert results.stored_results(tmp_path / "runs", "attacks")["available"] is False


def test_result_text_is_cleaned_and_served_as_data(tmp_path) -> None:
    folder = tmp_path / "runs" / "attacks"
    folder.mkdir(parents=True)
    (folder / "r.json").write_text(json.dumps({"note": "ok\u200b\x07 <script>alert(1)</script>"}))
    with Registry(tmp_path / "home") as registry:
        _, client, _ = make_dashboard(tmp_path, registry, runs_dir=tmp_path / "runs")
        res = client.get("/api/attacks")
        assert res.headers["content-type"].startswith("application/json")
        note = res.json()["latest"]["document"]["note"]
        assert note == "ok <script>alert(1)</script>"
        assert client.get("/api/bench").json()["available"] is False
        assert client.get("/api/attacks?name=" + "x" * 300).status_code == 422


def test_no_run_endpoint_exists(tmp_path) -> None:
    with Registry(Path(tmp_path) / "home") as registry:
        _, client, _ = make_dashboard(tmp_path, registry)
        for path in ("/api/attacks/run", "/api/bench/run", "/api/run"):
            assert client.post(path, json={}).status_code in (404, 405)
