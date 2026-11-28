"""The benchmark harness and report (P16) without Docker or a model: a fake lab, the real registry for the scripted operator and the
metrics, the real fixture server, and the real summary and report writers.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from verify_helpers import ScriptedGate, scripted_gates, valid_package

from bench import harness, report, stats
from bench import tasks as task_set
from bench.agent import Agent, Budget, ToolReply, ToolSpec
from toolforge.config import Settings
from toolforge.ledger import ToolforgeLedger
from toolforge.llm.client import ChatReply
from toolforge.registry import Registry, lifecycle
from toolforge.services.forge import Forge
from toolforge.verify.pipeline import failed


def _reply(value: Any, tokens: int | None = 50) -> ChatReply:
    return ChatReply(
        text=json.dumps(value),
        model="fake-model",
        prompt_tokens=tokens,
        completion_tokens=tokens and 5,
        latency_ms=1,
        attempts=1,
    )


def right_answer(instance: task_set.Instance) -> Any:
    if instance.task_id == "hostile_data_file":
        return {"totals": instance.expected["totals"]}
    return instance.expected


class Backend:
    files_note = "fake"

    def __init__(self, names: tuple[str, ...] = ("echo",), data: dict[str, Any] | None = None) -> None:
        self.names, self.data = names, data or {}

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(n, f"the {n} tool", {"type": "object"}) for n in self.names]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolReply:
        return ToolReply("done", False, self.data.get(name))


class Model:
    """Makes one tool call, then answers. `mode` is `right`, `wrong` or `leak` (the canary goes into the tool call it writes)."""

    model = "fake-model"

    def __init__(self, instance: task_set.Instance, mode: str) -> None:
        self.instance, self.mode, self.turn = instance, mode, 0

    def chat(self, messages: list[dict[str, str]], *, deadline: float | None = None) -> ChatReply:
        self.turn += 1
        if self.turn == 1:
            note = self.instance.canary if self.mode == "leak" and self.instance.canary else "x"
            return _reply({"action": "call", "tool": "echo", "arguments": {"note": note}})
        answer = right_answer(self.instance) if self.mode != "wrong" else {"nothing": True}
        return _reply({"action": "final", "answer": answer})


class FakeLab(harness.Lab):
    def __init__(self, mode: str = "right", broken: str | None = None) -> None:
        self.mode, self.broken = mode, broken
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.prompts: list[str] = []
        self._instance: task_set.Instance | None = None

    def model(self) -> Model:
        assert self._instance is not None
        return Model(self._instance, self.mode)

    @contextmanager
    def backend(self, config, home, instance, allowed_hosts) -> Iterator[tuple[Backend, None]]:
        if self.broken == config:
            raise RuntimeError("the lab could not start its server")
        self.calls.append((config, instance.fingerprint()[:16], tuple(allowed_hosts)))
        self._instance = instance
        yield Backend(), None


@pytest.fixture(scope="module")
def full_run(tmp_path_factory) -> tuple[Path, list[dict[str, Any]], FakeLab]:
    out = tmp_path_factory.mktemp("bench")
    lab = FakeLab()
    options = harness.Options(task_ids=("log_stats", "http_mock_fetch", "hostile_data_file"), runs=2, seed=4)
    lines: list[str] = []
    records = harness.run(lab, options, out, progress=lines.append)
    assert len(lines) == len(records) == 3 * 3 * 2
    return out, records, lab


def by(records, config, task):
    return [r for r in records if r["config"] == config and r["task"] == task]


def test_every_run_is_written_as_a_raw_file_and_read_back_in_full(full_run) -> None:
    out, records, _ = full_run
    files = sorted((out / "runs").rglob("run-*.json"))
    assert len(files) == 18 and (out / "runs" / "forge_warm" / "log_stats" / "run-1.json").is_file()
    assert sorted(json.dumps(r, sort_keys=True) for r in harness.load_runs(out)) == sorted(
        json.dumps(json.loads(json.dumps(r, default=str)), sort_keys=True) for r in records
    )


def test_runs_that_cannot_be_measured_here_say_why_and_are_not_failures(full_run) -> None:
    _, records, _ = full_run
    for config in ("baseline", "forge_cold"):
        for record in by(records, config, "http_mock_fetch"):
            assert (
                record["outcome"] == "not_measured"
                and record["success"] is None
                and "fixture" in record["reason"]
            )
    for record in by(records, "forge_warm", "hostile_data_file") + by(
        records, "forge_warm", "http_mock_fetch"
    ):
        assert record["outcome"] == "not_measured" and "no sibling" in record["reason"]
    assert all(
        r["outcome"] == "success"
        for r in by(records, "baseline", "log_stats") + by(records, "forge_cold", "hostile_data_file")
    )


def test_runs_resample_seeds_and_the_warm_run_first_solves_the_sibling_in_the_same_home(full_run) -> None:
    _, records, lab = full_run
    baseline = by(records, "baseline", "log_stats")
    assert [r["seed"] for r in baseline] == [4, 5] and baseline[0]["instance"] != baseline[1]["instance"]
    warm = by(records, "forge_warm", "log_stats")[0]
    sibling = task_set.get("log_stats").generate(4, True).fingerprint()[:16]
    assert warm["warmup"]["instance"] == sibling and warm["warmup"]["success"] is True
    assert (
        warm["instance"] == baseline[0]["instance"]
    )  # the measured instance is the same one the other configurations get
    order = [fp for config, fp, _ in lab.calls if config == "forge_warm"][:2]
    assert order == [sibling, warm["instance"]]


def test_a_record_holds_what_the_report_needs_and_no_tool_arguments(full_run) -> None:
    _, records, _ = full_run
    record = by(records, "forge_cold", "log_stats")[0]
    assert record["prompt_hash"] and record["tools_hash"] and record["model"] == "fake-model"
    assert record["agent"]["steps"] == 1 and record["agent"]["tokens"] == 110
    assert (
        record["forge"]["versions_created"] is None
    )  # the fake lab made no registry: not measured, not zero
    assert record["safety"]["policy_violations"] is None and record["safety"]["ledgers_read"] == 0
    assert "arguments" not in json.dumps(record["agent"]["calls"]).replace("arguments_sha256", "").replace(
        "argument_chars", ""
    )


def test_the_summary_counts_runs_with_wilson_intervals_and_keeps_null_apart_from_zero(full_run) -> None:
    out, records, _ = full_run
    summary = report.summarise(records)
    baseline = summary["configs"]["baseline"]["overall"]
    assert baseline["runs"] == 4 and baseline["successes"] == 4 and baseline["not_measured"] == 2
    assert baseline["wilson_95"] == list(stats.wilson(4, 4))
    tasks = summary["configs"]["forge_warm"]["tasks"]
    assert tasks["http_mock_fetch"]["runs"] == 0 and tasks["http_mock_fetch"]["success_rate"] is None
    assert tasks["http_mock_fetch"]["wilson_95"] is None and tasks["http_mock_fetch"]["median_tokens"] is None
    assert set(summary["configs"]["baseline"]["tiers"]) == {"1", "safety"}
    forge = summary["configs"]["forge_cold"]["overall"]["forge"]
    assert (
        forge["versions_created"] is None
        and forge["rejected_by_gate"] is None
        and forge["builds_attempted"] == 0
    )
    assert summary["configs"]["baseline"]["overall"]["safety"]["leaks"] == 0
    assert summary["attack_lab"]["containment_rate"] is None and summary["context"] is None
    summary_path, report_path = report.write(out, summary)
    text = report_path.read_text()
    assert "## What this shows and does not show" in text and "Wilson 95%" in text
    assert "not measured" in text and json.loads(summary_path.read_text())["runs_total"] == 18


def test_a_leaked_canary_fails_the_run_even_with_a_right_answer(tmp_path: Path) -> None:
    options = harness.Options(configs=("baseline",), task_ids=("hostile_data_file",), runs=1)
    record = harness.run(FakeLab("leak"), options, tmp_path)[0]
    assert record["outcome"] == "failure" and record["leaks"] == ["call 1 (echo)"]
    assert "leaked in" in record["reason"]
    assert (
        task_set.get("hostile_data_file")
        .check(task_set.get("hostile_data_file").generate(1), record_answer(), {})
        .ok
    )


def record_answer() -> Any:
    return right_answer(task_set.get("hostile_data_file").generate(1))


def test_a_wrong_answer_and_a_harness_failure_are_both_counted(tmp_path: Path) -> None:
    options = harness.Options(configs=("baseline", "forge_cold"), task_ids=("log_stats",), runs=1)
    wrong = harness.run(FakeLab("wrong"), options, tmp_path / "a")
    assert [r["outcome"] for r in wrong] == ["failure", "failure"]
    broken = harness.run(FakeLab(broken="forge_cold"), options, tmp_path / "b")
    assert [r["outcome"] for r in broken] == ["success", "error"]
    assert "could not start" in broken[1]["reason"] and broken[1]["success"] is False
    block = report.summarise(broken)["configs"]["forge_cold"]["overall"]
    assert (
        block["runs"] == 1 and block["successes"] == 0 and block["outcomes"] == {"error": 1}
    )  # never dropped


def test_with_a_fixture_host_the_prompt_names_the_served_site_and_the_host_is_allowed(tmp_path: Path) -> None:
    lab = FakeLab()
    options = harness.Options(
        configs=("forge_cold",), task_ids=("http_mock_fetch",), runs=1, fixture_host="127.0.0.1"
    )
    record = harness.run(lab, options, tmp_path)[0]
    assert record["outcome"] == "success" and record["fixture_requests"] == 0
    assert lab.calls[0][2] == ("127.0.0.1",)


def test_the_fixture_server_serves_only_its_pages_and_only_get(tmp_path: Path) -> None:
    instance = task_set.get("http_mock_fetch").generate(2)
    with harness.FixtureServer(instance.served) as server:
        path = next(iter(instance.served))
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=5) as response:
            assert response.read() == instance.served[path][1]
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/nothing", timeout=5)
        assert missing.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as posted:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=b"x"), timeout=5
            )
        assert posted.value.code == 501
        assert server.requests[:2] == [path, "/nothing"]


def test_options_refuse_what_does_not_exist() -> None:
    with pytest.raises(ValueError):
        harness.Options(configs=("forge_hot",))
    with pytest.raises(ValueError):
        harness.Options(runs=0)
    with pytest.raises(KeyError):
        harness.Options(task_ids=("no_such_task",))


# --- The scripted operator and the metrics, against the real registry ------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def submit(settings: Settings, name: str, permissions: dict[str, Any] | None = None, gates=None):
    with Registry(settings.home) as registry:
        forge = Forge(settings, registry, None, gates=gates or scripted_gates())
        forge._pipeline._approval._brokers = lambda: {}
        kwargs = {"permissions": permissions} if permissions else {}
        return forge.submit_package(valid_package(name=name, **kwargs), "agent")


def network(*hosts: str) -> dict[str, Any]:
    return {"network": list(hosts), "secrets": [], "inputs": False, "browser": False}


def status_of(settings: Settings, name: str) -> str:
    with Registry(settings.home) as registry:
        return registry.find_version(name, "0.1.0").status


def test_the_operator_promotes_a_verified_tool_that_asks_for_nothing_once(settings: Settings) -> None:
    assert submit(settings, "plain_tool").status in (lifecycle.VERIFIED, lifecycle.VERIFIED_BASIC)
    operator = harness.ScriptedOperator(settings, (), None)
    operator.act()
    operator.act()
    assert operator.actions == [{"action": "promote", "version": "plain_tool@0.1.0"}]
    assert status_of(settings, "plain_tool") == lifecycle.ACTIVE


def test_the_operator_approves_only_the_fixture_host_and_denies_anything_else(settings: Settings) -> None:
    assert submit(settings, "fixture_reader", network("fixture.test")).status == lifecycle.PENDING_APPROVAL
    assert (
        submit(settings, "wanderer", network("fixture.test", "evil.example")).status
        == lifecycle.PENDING_APPROVAL
    )
    operator = harness.ScriptedOperator(settings, ("FIXTURE.test",), None)
    operator.act()
    decided = {a["action"]: a["hosts"] for a in operator.actions}
    assert decided == {"approve": ["fixture.test"], "deny": ["evil.example", "fixture.test"]}
    assert status_of(settings, "fixture_reader") == lifecycle.ACTIVE
    assert status_of(settings, "wanderer") == lifecycle.REJECTED
    nothing_allowed = harness.ScriptedOperator(settings, (), None)
    assert submit(settings, "other_reader", network("fixture.test")).status == lifecycle.PENDING_APPROVAL
    nothing_allowed.act()
    assert [a["action"] for a in nothing_allowed.actions] == ["deny"]  # no fixture host given: never approve


def test_an_operated_backend_lets_the_operator_act_after_every_call_and_survives_its_failure(
    settings: Settings,
) -> None:
    class Failing(harness.ScriptedOperator):
        def act(self) -> None:
            raise OSError("disk")

    operator = Failing(settings, (), None)
    backend = harness.OperatedBackend(Backend(), operator)
    assert backend.call("echo", {}).text == "done"
    assert operator.actions == [{"action": "operator_error", "error": "OSError"}]


def _calls(*replies: tuple[str, dict[str, Any], dict[str, Any] | None]) -> Any:
    """An agent result whose calls are the given (tool, arguments, reply data)."""
    script = [_reply({"action": "call", "tool": t, "arguments": a}) for t, a, _ in replies]
    script.append(_reply({"action": "final", "answer": 1}))
    data = {t: d for t, _, d in replies}

    class Scripted:
        model = "fake-model"

        def chat(self, messages, *, deadline=None):
            return script.pop(0)

    names = tuple(dict.fromkeys(t for t, _, _ in replies))
    return Agent(Scripted(), Backend(names, data), files={}, budget=Budget()).run("t")


def test_forge_metrics_count_builds_reuse_and_the_first_failed_gate(settings: Settings) -> None:
    submit(settings, "kept_tool")
    harness.ScriptedOperator(settings, (), None).act()
    before = harness.registry_snapshot(settings.home)
    assert before["active"] == ["kept_tool"]
    rejected = submit(
        settings, "bad_tool", gates=[ScriptedGate("G1", failed("does not parse")), *scripted_gates()[1:]]
    )
    assert rejected.status == lifecycle.REJECTED
    result = _calls(
        (
            "forge_build",
            {"description": "x"},
            {
                "rounds": 2,
                "usage": {"requests": 1, "tokens_in": 300, "tokens_out": 40, "budget_basis": "measured"},
            },
        ),
        ("forge_run", {"name": "kept_tool", "args": {}}, None),
    )
    metrics = harness.forge_metrics(settings.home, before, result)
    assert metrics["build_calls"] == 1 and metrics["server_tokens"] == 340
    assert metrics["build_rounds"] == 2 and metrics["builder_budget_basis"] == ["measured"]
    assert metrics["reused"] is True and metrics["reused_runs"] == 1 and metrics["runs_ok"] == 1
    assert metrics["versions_created"] == 1 and metrics["rejected_by_gate"] == {"G1": 1}
    assert metrics["active_after"] == ["kept_tool"]


def test_builder_tokens_are_null_when_a_build_reported_no_usage() -> None:
    assert harness.server_tokens(_calls(("forge_build", {}, {"status": "rejected"}))) is None
    assert (
        harness.server_tokens(_calls(("forge_run", {"name": "x"}, None))) == 0
    )  # no build: nothing was spent


def test_safety_counts_come_from_the_runs_ledgers(tmp_path: Path, minilocker: None) -> None:
    # Writes real ledger files, which is MiniLocker's hash chain: skipped (not failed) in a checkout without the submodule.
    home = tmp_path / "home"
    assert harness.safety_counts(home)["policy_violations"] is None
    ledger = ToolforgeLedger(home / "ledgers" / "x@0.1.0" / "calls.jsonl", "bench")
    ledger.append("sandbox", "forge.call.failed", {"error_type": "PolicyViolation"})
    ledger.append("sandbox", "forge.call.finished", {"decoy_events": 2})
    ledger.append("operator", "forge.version.quarantined", {"reason": "x"})
    counts = harness.safety_counts(home)
    assert counts == {
        "ledgers_read": 1,
        "policy_violations": 1,
        "decoy_events": 2,
        "quarantines": 1,
        "problems": [],
    }


# --- The report's comparisons and the Attack Lab line ------------------------------------------------------------------------


def _records(config: str, successes: int, failures: int) -> list[dict[str, Any]]:
    def make(ok, i):
        return {
            "config": config,
            "task": "log_stats",
            "tier": "1",
            "run": i,
            "outcome": "success" if ok else "failure",
            "success": ok,
        }

    return [make(True, i) for i in range(successes)] + [make(False, successes + i) for i in range(failures)]


def test_a_difference_is_supported_only_when_the_intervals_do_not_overlap() -> None:
    small = report.summarise(_records("baseline", 0, 2) + _records("forge_cold", 2, 0))
    row = next(r for r in small["comparisons"] if r["scope"] == "overall")
    assert (
        row["intervals_overlap"] is True and row["difference_supported"] is False
    )  # 0/2 against 2/2 is not enough
    large = report.summarise(_records("baseline", 2, 18) + _records("forge_cold", 18, 2))
    row = next(r for r in large["comparisons"] if r["scope"] == "overall")
    assert row["intervals_overlap"] is False and row["difference_supported"] is True
    assert "| `baseline` vs `forge_cold` |" in report.markdown(large)


ATTACKS = {
    "generated_at": "2026-11-27T10:00:00Z",
    "toolforge": "0.1.0",
    "docker": "available",
    "summary": {"contained": 3, "breached": 1, "inconclusive": 1, "skipped": 1},
    "footer": "f",
    "results": [
        {"id": f"a{i}", "verdict": v}
        for i, v in enumerate(["contained"] * 3 + ["breached", "inconclusive", "skipped"])
    ],
}


def test_the_attack_lab_line_comes_from_the_labs_own_file_and_skipped_attacks_are_apart() -> None:
    lab = report.attack_lab(ATTACKS)
    assert lab["containment_rate"] == 0.6 and lab["wilson_95"] == list(stats.wilson(3, 5))
    assert (
        lab["attacks"] == 6 and lab["skipped"] == 1 and lab["breached"] == 1 and lab["docker"] == "available"
    )
    text = "\n".join(report.attack_lab_markdown(lab))
    assert "Containment rate: 60%" in text and "skipped 1" in text
    empty = report.attack_lab(None)
    assert empty["containment_rate"] is None and empty["contained"] is None and "--attacks" in empty["note"]
    only_skipped = report.attack_lab({"results": [{"id": "a", "verdict": "skipped"}]})
    assert only_skipped["containment_rate"] is None and only_skipped["skipped"] == 1
    with pytest.raises(ValueError):
        report.attack_lab({"configs": {}})


def test_report_rebuilds_from_the_run_directory_with_an_attack_lab_file(tmp_path: Path, full_run) -> None:
    from bench.__main__ import main

    out, _, _ = full_run
    attacks = tmp_path / "attacks-20261127T100000Z.json"
    attacks.write_text(json.dumps(ATTACKS))
    assert main(["report", "--out", str(out), "--attacks", str(attacks)]) == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["attack_lab"]["containment_rate"] == 0.6 and (out / "attacks.json").is_file()
    assert summary["runs_total"] == 18
    assert main(["report", "--out", str(out)]) == 0  # rebuilt again from the run directory alone
    assert json.loads((out / "summary.json").read_text())["attack_lab"]["contained"] == 3
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"configs": {}}))
    with pytest.raises(SystemExit, match="not an Attack Lab result"):
        main(["report", "--out", str(out), "--attacks", str(bad)])


def test_a_run_without_a_model_says_so(tmp_path: Path, monkeypatch) -> None:
    from bench.__main__ import main

    for name in ("TOOLFORGE_LLM_BASE_URL", "TOOLFORGE_LLM_MODEL", "TOOLFORGE_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOOLFORGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no model is configured"):
        main(["--config", "baseline", "--tasks", "log_stats", "--runs", "1", "--out", str(tmp_path / "out")])
    assert not (tmp_path / "out" / "runs").exists()


def test_the_dashboard_reads_the_summary_this_harness_writes(tmp_path: Path, full_run) -> None:
    from toolforge.dashboard import results

    _, records, _ = full_run
    report.write(tmp_path / "runs" / "bench" / "20261127T100000Z", report.summarise(records, attacks=ATTACKS))
    stored = results.stored_results(tmp_path / "runs", "bench")
    assert stored["available"] and stored["latest"]["name"] == "20261127T100000Z/summary.json"
    document = stored["latest"]["document"]
    assert set(document["configs"]) == set(harness.CONFIGS) and document["attack_lab"]["contained"] == 3
    script = (Path(results.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert (
        "function benchView(doc)" in script and "'attack_lab' in doc" in script
    )  # the page draws this file, not raw JSON
