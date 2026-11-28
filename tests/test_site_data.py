"""P19: `scripts/build_site_data.py` and what the page does with its output. A missing result is never a zero."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from site_helpers import ROOT, build_data, build_dist, numbers_in, parse, sample_runs, script

from toolforge.attacks.catalog import ATTACKS

data = script("build_site_data")
NOT_MEASURED = "Not measured yet"


def _empty_demo(tmp_path: Path) -> Path:
    return tmp_path / "no-demo"


# --- The data file ---------------------------------------------------------------------------------------------------------------


def test_with_nothing_measured_every_source_is_null(tmp_path: Path) -> None:
    doc = data.build(tmp_path / "runs", "2026-11-21", _empty_demo(tmp_path))
    assert doc == {
        "schema": 1,
        "generated_at": "2026-11-21",
        "attacks": None,
        "bench": None,
        "context": None,
        "demo": None,
    }


def test_the_committed_snapshot_is_valid_and_honest() -> None:
    snapshot = json.loads((ROOT / "site" / "data" / "results.json").read_text(encoding="utf-8"))
    assert snapshot["schema"] == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", snapshot["generated_at"])
    assert set(snapshot) == {"schema", "generated_at", "attacks", "bench", "context", "demo"}
    # No benchmark or attack result is committed to the repository (README, docs/KNOWN_LIMITATIONS.md).
    assert snapshot["attacks"] is None and snapshot["bench"] is None and snapshot["context"] is None
    assert snapshot["demo"]["isolated"] is False, "the committed recording was made with the stand-in sandbox"


def test_sample_runs_are_read_into_the_documented_shape(tmp_path: Path) -> None:
    doc = build_data(sample_runs(tmp_path), tmp_path / "out.json")
    attacks = doc["attacks"]
    assert attacks["total"] == len(ATTACKS) == len(attacks["per_attack"])
    assert (attacks["contained"], attacks["breached"], attacks["inconclusive"], attacks["skipped"]) == (
        len(ATTACKS) - 2,
        0,
        1,
        1,
    )
    assert attacks["docker"] == "27.0.1"
    assert set(attacks["per_attack"].values()) == {"contained", "inconclusive", "skipped"}
    bench = doc["bench"]
    assert list(bench["configs"]) == ["baseline", "forge_cold", "forge_warm"]
    assert bench["configs"]["forge_warm"]["overall"]["successes"] == 4
    assert bench["configs"]["baseline"]["overall"]["wilson_95"] == pytest.approx(
        [0.0362, 0.6245], abs=1e-3
    )  # Wilson, 1 of 5, worked by hand
    assert bench["date"] == "2026-11-20"
    context = doc["context"]
    assert context["method"] == "estimate"
    assert sorted(context["sizes"], key=int) == ["10", "100"]
    assert context["sizes"]["10"]["meta"]["tokens"] > 0
    assert doc["demo"]["sandbox"] == "stand-in"


def test_the_newest_run_wins_and_the_clock_is_not_used(tmp_path: Path) -> None:
    runs = sample_runs(tmp_path)
    older = runs / "attacks" / "attacks-20200101T000000Z.json"
    newest = sorted((runs / "attacks").glob("attacks-*.json"))[-1]
    older.write_text(
        newest.read_text(encoding="utf-8").replace('"contained"', '"breached"'), encoding="utf-8"
    )
    assert data.load_attacks(runs)["breached"] == 0
    newest.rename(runs / "attacks" / "attacks-19990101T000000Z.json")
    assert data.load_attacks(runs)["breached"] == len(ATTACKS) - 2, (
        "now the file that was made to look newer is the newest"
    )


def test_a_source_in_the_wrong_shape_is_null_never_zero(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "attacks").mkdir(parents=True)
    (runs / "attacks" / "attacks-20261121T000000Z.json").write_text("{not json", encoding="utf-8")
    (runs / "bench" / "x").mkdir(parents=True)
    (runs / "bench" / "x" / "summary.json").write_text(
        json.dumps({"schema": 2, "configs": {}}), encoding="utf-8"
    )
    assert data.build(runs, "2026-11-21", _empty_demo(tmp_path))["attacks"] is None
    assert data.build(runs, "2026-11-21", _empty_demo(tmp_path))["bench"] is None
    (runs / "attacks" / "attacks-20261121T000000Z.json").write_text(
        json.dumps({"results": [{"id": "a", "verdict": "contained"}, {"id": "b"}]}), encoding="utf-8"
    )
    assert data.load_attacks(runs) is None, "a row without a verdict makes the whole file unusable"


def test_the_output_is_deterministic(tmp_path: Path) -> None:
    runs = sample_runs(tmp_path)
    build_data(runs, tmp_path / "a.json")
    build_data(runs, tmp_path / "b.json")
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


def test_a_recording_that_fails_its_own_check_is_not_used(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good"
    shutil.copytree(ROOT / "demo" / "recordings" / "main", good)
    assert data.load_demo(good)["toolforge_version"]
    bad = tmp_path / "bad"
    shutil.copytree(good, bad)
    meta = json.loads((bad / "recording.json").read_text(encoding="utf-8"))
    meta["schema"] = 99
    (bad / "recording.json").write_text(json.dumps(meta), encoding="utf-8")
    assert data.load_demo(bad) is None
    assert "schema must be 1" in capsys.readouterr().err
    leaky = tmp_path / "leaky"
    shutil.copytree(good, leaky)
    events = leaky / "events.json"
    events.write_text(
        events.read_text(encoding="utf-8").replace("{", '{"leak": "AKIAABCDEFGHIJKLMNOP", ', 1),
        encoding="utf-8",
    )
    assert data.load_demo(leaky) is None


# --- What the page does with it --------------------------------------------------------------------------------------------------


def _proof(dist: Path):
    page = parse(dist / "index.html")
    return page, " ".join(page.section_text["proof"])


def test_with_nothing_measured_the_evidence_shows_no_figure(tmp_path: Path) -> None:
    dist = build_dist(
        tmp_path, results=json.loads((ROOT / "site" / "data" / "results.json").read_text(encoding="utf-8"))
    )
    page, text = _proof(dist)
    assert page.section_html_fig["proof"] == [], "a figure appeared with nothing measured"
    assert "%" not in text
    assert text.count(NOT_MEASURED) >= 3
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert html.count('class="nm-block"') >= 2
    assert "No benchmark has been run" in html
    assert re.findall(r'<td class="result"><span class="nm">', html), (
        "unmeasured attacks use the hatched state"
    )
    assert 'class="verdict' not in html


def test_with_results_every_figure_comes_from_results_json(tmp_path: Path) -> None:
    results = build_data(sample_runs(tmp_path), tmp_path / "r.json")
    dist = build_dist(tmp_path, results=results)
    page, text = _proof(dist)
    figures = page.section_html_fig["proof"]
    assert len(figures) >= 20
    allowed = numbers_in(results)
    for figure in figures:
        assert figure in allowed, f"{figure!r} is on the page but cannot be derived from results.json"
    assert "contained" in text and "inconclusive" in text
    assert "27.0.1" in text
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert 'class="nm"' not in html and "nm-block" not in html, (
        "everything was measured, so no hatched state should remain"
    )


def test_a_missing_figure_inside_a_result_is_never_drawn_as_zero(tmp_path: Path) -> None:
    results = {
        "schema": 1,
        "generated_at": "2026-11-21",
        "attacks": None,
        "context": None,
        "demo": None,
        "bench": {
            "run_id": "x",
            "date": "2026-11-20",
            "model": "m",
            "runs": 3,
            "configs": {
                "baseline": {
                    "overall": {
                        "runs": 3,
                        "successes": 0,
                        "success_rate": None,
                        "wilson_95": None,
                        "not_measured": 3,
                    },
                    "tiers": {},
                }
            },
            "comparisons": [],
        },
    }
    dist = build_dist(tmp_path, results=results)
    page, text = _proof(dist)
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert "%" not in text
    assert html.count('<td class="result"><span class="nm">Not measured yet</span></td>') >= 2
    assert page.section_html_fig["proof"][:3] == ["3", "3", "0"]


def test_an_attack_the_results_do_not_know_stays_not_measured(tmp_path: Path) -> None:
    results = build_data(sample_runs(tmp_path, bench=False, context=False), tmp_path / "r.json")
    dropped = ATTACKS[-1].id
    del results["attacks"]["per_attack"][dropped]
    dist = build_dist(tmp_path, results=results)
    html = (dist / "index.html").read_text(encoding="utf-8")
    row = re.search(rf"<code>{dropped}</code>.*?</tr>", html, re.S)[0]
    assert NOT_MEASURED in row
    other = re.search(rf"<code>{ATTACKS[2].id}</code>.*?</tr>", html, re.S)[0]
    assert NOT_MEASURED not in other and "contained" in other


def test_the_context_table_states_its_method_and_that_it_is_synthetic(tmp_path: Path) -> None:
    results = build_data(sample_runs(tmp_path, attacks=False, bench=False), tmp_path / "r.json")
    dist = build_dist(tmp_path, results=results)
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert "synthetic tools" in html
    assert "<strong>estimate</strong>" in html
    assert "tokens, estimate" in html
