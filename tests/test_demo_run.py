"""The whole demo, end to end, against the real server, gates and ledger with the stand-in sandbox (no Docker needed).

The hostile fixtures never run: gate G1 rejects the first, and the stand-in refuses the marker anyway. The decoy part of act 3 needs a real sandbox and is
not exercised here (it is a turn that `requires: isolation`).
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pytest

from demo import acts, hostile, recording, run_demo, runner

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = ROOT / "demo" / "transcripts"
FIXED = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def played(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    out = tmp_path_factory.mktemp("demo")
    record = out / "recording"
    stream = io.StringIO()
    options = runner.Options(
        transcripts=TRANSCRIPTS,
        sandbox="stand-in",
        out=out / "runs",
        record=record,
        stream=stream,
        now=lambda: FIXED,
    )
    code, summary = runner.run(options)
    return {"code": code, "summary": summary, "text": stream.getvalue(), "record": record, "out": options.out}


def test_every_act_passes_and_the_exit_code_says_so(played: dict[str, object]) -> None:
    assert played["code"] == 0
    summary = played["summary"]
    assert [a["checker"] for a in summary["acts"]] == ["accepted"] * 4  # type: ignore[index]


def test_the_screen_says_plainly_that_it_is_a_replay_with_no_isolation(played: dict[str, object]) -> None:
    text = str(played["text"])
    assert "REPLAY: the agent's replies are scripted" in text and "no language model is involved" in text
    assert "STAND-IN. No isolation" in text and "NOT shown" in text


def test_reuse_takes_fewer_steps_and_builds_nothing(played: dict[str, object]) -> None:
    by_id = {a["act"]: a for a in played["summary"]["acts"]}  # type: ignore[index]
    assert by_id["reuse"]["steps"] < by_id["forge"]["steps"]
    assert by_id["reuse"]["tokens_estimated"] < by_id["forge"]["tokens_estimated"]
    assert "Act 1 next to act 2" in str(played["text"])


def test_the_hostile_package_is_rejected_by_g1_and_never_promoted(played: dict[str, object]) -> None:
    from toolforge.config import Settings
    from toolforge.registry import Registry

    home = Path(str(played["out"])) / "home"
    with Registry(Settings(home=home).home) as registry:
        [version] = registry.list_versions(name=hostile.ATTEMPT_ONE)
        assert version.status == "rejected"
        failed = [g.gate for g in registry.gate_results(version.id) if g.status == "failed"]
        assert failed == ["G1"]
        assert registry.list_versions(name=hostile.ATTEMPT_TWO) == [], (
            "the quiet attempt needs a real sandbox and is left out here"
        )
        assert registry.active_version("csv_totals") is not None
        assert registry.active_version(hostile.ATTEMPT_ONE) is None
    assert "REJECTED" in str(played["text"])


def test_the_operator_promoted_only_the_honest_tools(played: dict[str, object]) -> None:
    promoted = [a["version"] for a in played["summary"]["operator"] if a["action"] == "promote"]  # type: ignore[index]
    assert promoted == ["sqlite_query@1.0.0", "csv_totals@1.0.0", "blackjack_session@1.0.0"]


def test_promotions_are_in_the_ledger(played: dict[str, object]) -> None:
    ledgers = Path(str(played["out"])) / "home" / "ledgers"
    lines = [json.loads(x) for p in ledgers.rglob("operator.jsonl") for x in p.read_text().splitlines()]
    assert sorted(e["type"] for e in lines) == ["forge.promoted"] * 3


def test_the_run_folder_has_the_summary_and_the_ledgers(played: dict[str, object]) -> None:
    out = Path(str(played["out"]))
    assert out.name == "20260102T030405Z" and (out / "summary.json").is_file() and (out / "ledgers").is_dir()


def test_the_recording_it_wrote_validates_and_is_labelled(played: dict[str, object]) -> None:
    record = Path(str(played["record"]))
    assert recording.validate(record) == []
    meta = json.loads((record / "recording.json").read_text(encoding="utf-8"))
    assert (meta["mode"], meta["agent"], meta["sandbox"], meta["isolated"], meta["recorded_at"]) == (
        "replay",
        "scripted",
        "stand-in",
        False,
        "2026-01-02",
    )
    assert meta["g5"] == "skipped" and "no isolation" in meta["note"]


def test_two_replays_give_the_same_verdicts_and_the_same_ledger_event_sequence(
    played: dict[str, object], tmp_path: Path
) -> None:
    again = runner.Options(
        transcripts=TRANSCRIPTS,
        sandbox="stand-in",
        out=tmp_path,
        record=tmp_path / "rec",
        stream=io.StringIO(),
        now=lambda: FIXED,
    )
    runner.run(again)

    def shape(directory: Path) -> list[tuple[str, str, str]]:
        events = json.loads((directory / "events.json").read_text(encoding="utf-8"))["events"]
        return [(e["act"], e["tool"], e["type"]) for e in events if e["ledger"] != "box"]

    assert shape(tmp_path / "rec") == shape(Path(str(played["record"])))


def test_a_script_that_the_real_system_contradicts_stops_with_an_error(tmp_path: Path) -> None:
    import shutil

    broken = tmp_path / "t"
    shutil.copytree(TRANSCRIPTS, broken)
    path = broken / "02-reuse.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["turns"][1]["expect"] = ["a sentence the server never says"]
    path.write_text(json.dumps(data), encoding="utf-8")
    options = runner.Options(
        transcripts=broken,
        sandbox="stand-in",
        out=tmp_path / "o",
        stream=io.StringIO(),
        acts=acts.select("1,2"),
    )
    with pytest.raises(runner.DemoError, match="answered differently"):
        runner.run(options)


def test_the_command_line_refuses_what_cannot_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_demo.main(["--replay", str(TRANSCRIPTS), "--acts", "2", "--out", str(tmp_path)]) == 2
    assert "reuses what act 1 forges" in capsys.readouterr().err
    assert run_demo.main(["--live", "--sandbox", "stand-in", "--out", str(tmp_path)]) == 2
    assert "needs --sandbox docker" in capsys.readouterr().err
    assert (
        run_demo.main(["--replay", str(tmp_path / "none"), "--sandbox", "stand-in", "--out", str(tmp_path)])
        == 2
    )
    assert (
        run_demo.main(
            [
                "--replay",
                str(TRANSCRIPTS),
                "--sandbox",
                "stand-in",
                "--out",
                str(tmp_path),
                "--record",
                str(tmp_path),
            ]
        )
        == 2
    ), "a recording directory that holds other files is refused"


def test_the_default_sandbox_without_a_docker_daemon_stops_with_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import docker

    def refuse(*args: object, **kwargs: object) -> None:
        raise docker.errors.DockerException("no daemon here")

    monkeypatch.setattr(docker, "from_env", refuse)
    assert run_demo.main(["--replay", str(TRANSCRIPTS), "--out", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert (
        "no Docker daemon answered" in err and "--sandbox stand-in" in err and "toolforge images build" in err
    )
