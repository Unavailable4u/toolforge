"""`toolforge attacks list|run`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolforge.attacks import catalog, runner
from toolforge.attacks.catalog import CONTAINED, INCONCLUSIVE, SKIPPED, attack_ids
from toolforge.attacks.runner import AttackResult
from toolforge.cli import main


def out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


def test_list_names_every_attack_and_whether_it_needs_docker(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["attacks", "list"]) == 0
    text = out(capsys)
    for attack in catalog.ATTACKS:
        assert attack.id in text and attack.title in text
    assert "no docker" in text and f"{len(catalog.ATTACKS)} attacks" in text


def test_list_markdown_prints_exactly_the_documented_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["attacks", "list", "--markdown"]) == 0
    assert out(capsys).strip() == catalog.attacks_markdown()


def test_run_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["attacks"])


def fake_run(monkeypatch: pytest.MonkeyPatch, verdicts: list[str]) -> list[dict[str, object]]:
    """Replace the real runner with scripted results and record how it was called."""
    calls: list[dict[str, object]] = []

    def run(
        only: object = None, *, deadline_s: float, progress: object = None, **_: object
    ) -> list[AttackResult]:
        calls.append({"only": only, "deadline": deadline_s})
        chosen = only or list(attack_ids())[: len(verdicts)]
        return [
            AttackResult(i, i, v, f"{v} evidence", [], 0.0) for i, v in zip(chosen, verdicts, strict=False)
        ]

    monkeypatch.setattr(runner, "run_attacks", run)
    monkeypatch.setattr(runner, "docker_problem", lambda settings: "needs Docker: no daemon (test)")
    return calls


def test_run_prints_the_table_and_exits_zero_when_everything_held(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = fake_run(monkeypatch, [CONTAINED, CONTAINED])
    assert main(["attacks", "run"]) == 0
    text = out(capsys)
    assert "contained 2/2" in text and "not about all possible attacks" in text
    assert calls == [{"only": None, "deadline": runner.DEADLINE_S}]


def test_only_and_deadline_are_passed_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = fake_run(monkeypatch, [CONTAINED, CONTAINED])
    assert main(["attacks", "run", "--only", "decoy_read", "--only", "cpu_loop", "--deadline", "30"]) == 0
    assert calls == [{"only": ["decoy_read", "cpu_loop"], "deadline": 30.0}]


def test_exit_code_is_two_when_something_is_unproven_or_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, [CONTAINED, INCONCLUSIVE])
    assert main(["attacks", "run"]) == 2
    fake_run(monkeypatch, [CONTAINED, SKIPPED])
    assert main(["attacks", "run"]) == 2


def test_exit_code_is_one_when_anything_was_breached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, [CONTAINED, "breached", INCONCLUSIVE])
    assert main(["attacks", "run"]) == 1


def test_json_writes_under_runs_attacks_in_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake_run(monkeypatch, [CONTAINED])
    monkeypatch.chdir(tmp_path)
    assert main(["attacks", "run", "--json"]) == 0
    [written] = (tmp_path / "runs" / "attacks").glob("attacks-*.json")
    document = json.loads(written.read_text(encoding="utf-8"))
    assert document["summary"]["contained"] == 1 and document["docker"].startswith("needs Docker")
    assert f"wrote {Path('runs') / 'attacks' / written.name}" in out(capsys)


def test_an_unknown_attack_is_a_clear_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["attacks", "run", "--only", "no_such_attack"]) != 0
    err = capsys.readouterr().err
    assert "no_such_attack" in err and "toolforge attacks list" in err


def test_the_command_is_listed_in_the_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "attacks" in out(capsys)
