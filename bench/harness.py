"""Runs the benchmark: configurations x tasks x runs, one fresh ToolForge home per run, one raw JSON file per run.

The three configurations (`docs/BENCHMARK.md`):

* `baseline`: `run_python` only (`agent.PythonBackend`), through the same `ToolRunner` and sandbox as ToolForge, no registry.
* `forge_cold`: the ToolForge MCP tools over stdio (a `toolforge serve` child process) and an empty registry.
* `forge_warm`: the same, in a home where the agent first ran the task's **sibling** (same capability, other data). The warm-up run is
  kept in the raw files (`warmup`) and never counted. A task without a sibling has no warm configuration: its runs are `not_measured`.

**The scripted operator.** ToolForge never makes an agent's tool runnable by itself: a verified version waits for an operator to promote
it, and a version that asks for network waits for approval (P09). Nobody is at the keyboard during a benchmark, so the harness plays a
fixed operator (`ScriptedOperator`) after every tool call in the forge configurations, and records every action in the run file:

* promote a `verified` or `verified_basic` version that asks for no network and no secrets (`operator_actions.promote`, which still
  refuses one whose build gate did not pass);
* approve a pending network request only when every host it names is one the task allows (the fixture host), and it asks for no secret;
  deny every other request. Approval goes through `operator_actions.decide`, so its own checks (gates, live examples) all apply.

That is more generous than a careful human (who would read the code first) and stricter than none. The numbers are for this operator.

**What is counted.** A run succeeds when the agent gave a final answer, the hidden checker accepts it, and nothing leaked. A run that
could not be measured here (a task that needs the fixture site without `--fixture-host`, or a warm run of a task with no sibling) is
`not_measured` with the reason; it is never a failure and never dropped from the tables. Every other run counts, whatever went wrong
(budget, refusal, crash of the harness's own plumbing: `error`).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bench import tasks as task_set
from bench.agent import (
    Agent,
    AgentResult,
    Budget,
    ChatModel,
    ToolBackend,
    ToolReply,
    ToolSpec,
    prompt_hash,
    tools_hash,
)

CONFIGS = ("baseline", "forge_cold", "forge_warm")
FORGE_CONFIGS = ("forge_cold", "forge_warm")
BUILD_TOOLS = ("forge_submit", "forge_build", "forge_repair")
RUN_TOOLS = ("forge_run", "forge_chain")
RESULT_SCHEMA = 1
OPERATOR_REASON = (
    "benchmark scripted operator (docs/BENCHMARK.md): verified and asks for no network or secrets"
)


@dataclass(frozen=True)
class Options:
    configs: tuple[str, ...] = CONFIGS
    task_ids: tuple[str, ...] = ()
    runs: int = 5
    seed: int = 1
    budget: Budget = field(default_factory=Budget)
    fixture_host: str | None = None  # a name that reaches the fixture server from inside the sandbox
    fixture_bind: str = "127.0.0.1"
    fixture_port: int = 0
    keep_homes: bool = False

    def __post_init__(self) -> None:
        unknown = [c for c in self.configs if c not in CONFIGS]
        if unknown:
            raise ValueError(
                f"unknown configuration {unknown[0]!r}; the configurations are: {', '.join(CONFIGS)}"
            )
        if self.runs < 1:
            raise ValueError("--runs must be at least 1")
        for task_id in self.task_ids:
            task_set.get(task_id)

    def tasks(self) -> list[task_set.Task]:
        return [task_set.get(t) for t in self.task_ids] if self.task_ids else list(task_set.TASKS)


# --- The scripted operator ---------------------------------------------------------------------------------------------------


class ScriptedOperator:
    """Promotes and approves as described in the module docstring. Every action (or refusal) is appended to `actions`."""

    def __init__(
        self, settings: Any, allowed_hosts: Sequence[str], runner_factory: Callable[[], Any] | None
    ) -> None:
        self.settings = settings
        self.allowed = {h.lower() for h in allowed_hosts}
        self.runner_factory = runner_factory
        self.actions: list[dict[str, Any]] = []
        self._seen: set[tuple[str, int]] = set()

    def act(self) -> None:
        from toolforge.errors import ToolForgeError
        from toolforge.registry import Registry
        from toolforge.registry import lifecycle as lc
        from toolforge.services import approvals, operator_actions

        registry = Registry.from_settings(self.settings)
        try:
            for status in (lc.VERIFIED, lc.VERIFIED_BASIC):
                for record in registry.list_versions(status=status):
                    if record.asks_network_or_secrets or ("promote", record.id) in self._seen:
                        continue
                    self._seen.add(("promote", record.id))
                    try:
                        operator_actions.promote(self.settings, registry, record.label, OPERATOR_REASON)
                        self.actions.append({"action": "promote", "version": record.label})
                    except ToolForgeError as exc:
                        self.actions.append(
                            {"action": "promote_refused", "version": record.label, "code": exc.code}
                        )
            for approval in registry.list_approvals("pending"):
                if ("decide", approval.id) in self._seen:
                    continue
                self._seen.add(("decide", approval.id))
                record = registry.get_version(approval.version_id)
                asked = approvals.requested_permissions(record.manifest if record else {})
                hosts = {h.lower() for h in asked["network"]}
                fits = bool(hosts) and hosts <= self.allowed and not asked["secrets"]
                decision = "approve" if fits else "deny"
                reason = (
                    "benchmark scripted operator: every host is the task's fixture host"
                    if fits
                    else "benchmark scripted operator: asks for a host the task does not allow, or for a secret"
                )
                try:
                    operator_actions.decide(
                        self.settings,
                        registry,
                        approval.id,
                        decision,
                        reason,
                        tool_runner_factory=self.runner_factory,
                    )
                    self.actions.append(
                        {"action": decision, "approval": approval.id, "hosts": sorted(hosts)[:8]}
                    )
                except ToolForgeError as exc:
                    self.actions.append(
                        {"action": f"{decision}_refused", "approval": approval.id, "code": exc.code}
                    )
        finally:
            registry.close()


class OperatedBackend:
    """A backend whose every call is followed by the scripted operator's turn."""

    def __init__(self, inner: ToolBackend, operator: ScriptedOperator) -> None:
        self.inner, self.operator = inner, operator
        self.files_note = inner.files_note

    def tools(self) -> list[ToolSpec]:
        return self.inner.tools()

    def call(self, name: str, arguments: dict[str, Any]) -> ToolReply:
        reply = self.inner.call(name, arguments)
        try:
            self.operator.act()
        except Exception as exc:  # the operator is plumbing: its failure is recorded, the run goes on
            self.operator.actions.append({"action": "operator_error", "error": type(exc).__name__})
        return reply


# --- The fixture server ------------------------------------------------------------------------------------------------------


class FixtureServer:
    """Serves one instance's `served` pages over plain HTTP, read-only. Bound to `bind` (loopback by default); GET only."""

    def __init__(
        self, pages: Mapping[str, tuple[str, bytes]], bind: str = "127.0.0.1", port: int = 0
    ) -> None:
        self.pages = dict(pages)
        self.requests: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (the standard library's name)
                owner.requests.append(self.path[:200])
                page = owner.pages.get(self.path)
                if page is None:
                    self.send_error(404)
                    return
                kind, body = page
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> FixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


# --- Labs: what makes a configuration's tools ---------------------------------------------------------------------------------


class Lab:
    """Makes the model and, per run, the tools of a configuration. `RealLab` is the real thing; tests pass a fake."""

    def model(self) -> ChatModel:
        raise NotImplementedError

    def backend(
        self, config: str, home: Path, instance: task_set.Instance, allowed_hosts: Sequence[str]
    ) -> AbstractContextManager[tuple[ToolBackend, ScriptedOperator | None]]:
        raise NotImplementedError


class RealLab(Lab):
    """The real configurations: the LLM endpoint from Settings, the MiniLocker sandbox, `toolforge serve` over stdio."""

    def __init__(self, settings: Any) -> None:
        from toolforge.llm.client import client_from_settings

        client = client_from_settings(settings, "builder")
        if client is None:
            raise RuntimeError(
                "no model is configured: set TOOLFORGE_LLM_BASE_URL and TOOLFORGE_LLM_MODEL (see docs/BENCHMARK.md)"
            )
        self._client = client
        self._settings = settings

    def model(self) -> ChatModel:
        return self._client

    def _settings_for(self, home: Path) -> Any:
        return self._settings.model_copy(update={"home": home})

    def _runner(self, settings: Any) -> Any:
        from toolforge import cli_run
        from toolforge.services.runner import ToolRunner

        return ToolRunner(cli_run.make_sandbox_runner(settings), None, settings, cli_run.make_ledger)

    @contextmanager
    def backend(
        self, config: str, home: Path, instance: task_set.Instance, allowed_hosts: Sequence[str]
    ) -> Iterator[tuple[ToolBackend, ScriptedOperator | None]]:
        from bench.agent import McpBackend, PythonBackend

        settings = self._settings_for(home)
        if config == "baseline":
            runner = self._runner(settings)
            try:
                yield PythonBackend(runner, instance.files), None
            finally:
                stop = getattr(getattr(runner, "_runner", None), "shutdown", None)
                if stop is not None:
                    stop()
            return
        from mcp import StdioServerParameters

        env = dict(_inherited_environment())
        env["TOOLFORGE_HOME"] = str(home)
        server = StdioServerParameters(
            command=sys.executable, args=["-m", "toolforge.cli", "serve"], env=env, cwd=str(home)
        )
        operator = ScriptedOperator(settings, allowed_hosts, lambda: self._runner(settings))
        with McpBackend(server) as backend:
            yield OperatedBackend(backend, operator), operator


def _inherited_environment() -> dict[str, str]:
    """The child server's environment: the harness's own, so it reads the same TOOLFORGE_* settings (Settings reads them there)."""
    import os

    return dict(os.environ)


# --- One run --------------------------------------------------------------------------------------------------------------


def registry_snapshot(home: Path) -> dict[str, Any]:
    """Which versions exist and which tools are active, before a run. Empty when the home has no registry yet."""
    from toolforge.registry import DB_NAME, Registry

    if not (home / DB_NAME).exists():
        return {"version_ids": [], "active": []}
    registry = Registry(home)
    try:
        versions = registry.list_versions()
        active = sorted({v.name for v in versions if v.status == "active"})
        return {"version_ids": sorted(v.id for v in versions), "active": active}
    finally:
        registry.close()


def forge_metrics(home: Path, before: Mapping[str, Any], result: AgentResult) -> dict[str, Any]:
    """Builds, gate rejections and reuse in one forge run, from the registry and the agent's calls (never from its messages)."""
    from toolforge.registry import DB_NAME, Registry

    build_calls = sum(1 for c in result.calls if c.tool in BUILD_TOOLS)
    runs = [c for c in result.calls if c.tool == "forge_run"]
    preexisting = set(before.get("active", []))
    reused = 0
    for call in runs:
        try:
            name = json.loads(call.authored).get("name")
        except (ValueError, AttributeError):
            name = None
        if call.ok and name in preexisting:
            reused += 1
    builds = [
        c.data for c in result.calls if c.tool in ("forge_build", "forge_repair") and isinstance(c.data, dict)
    ]
    rounds = [
        b.get("rounds")
        for b in builds
        if isinstance(b.get("rounds"), int) and not isinstance(b.get("rounds"), bool)
    ]
    bases = {
        str(b["usage"].get("budget_basis"))
        for b in builds
        if isinstance(b.get("usage"), dict) and b["usage"].get("budget_basis")
    }
    metrics: dict[str, Any] = {
        "build_calls": build_calls,
        "server_tokens": server_tokens(result),
        "build_rounds": sum(rounds) if rounds else (0 if not builds else None),
        "builder_budget_basis": sorted(
            bases
        ),  # P11: an `estimated` budget is an estimate; the token figures above are reported usage only
        "run_calls": len(runs),
        "runs_ok": sum(1 for c in runs if c.ok),
        "reused_runs": reused,
        "reused": reused > 0,
        "versions_created": None,
        "rejected_by_gate": None,
        "active_after": None,
    }
    if not (home / DB_NAME).exists():
        return metrics
    registry = Registry(home)
    try:
        old = set(before.get("version_ids", []))
        new = [v for v in registry.list_versions() if v.id not in old]
        rejected: dict[str, int] = {}
        for version in new:
            if version.status != "rejected":
                continue
            failed = [g.gate for g in registry.gate_results(version.id) if g.status == "failed"]
            gate = (
                failed[0] if failed else "no_gate"
            )  # rejected without a failed gate: refused or denied before or after gates
            rejected[gate] = rejected.get(gate, 0) + 1
        metrics["versions_created"] = len(new)
        metrics["rejected_by_gate"] = dict(sorted(rejected.items()))
        metrics["active_after"] = sorted({v.name for v in registry.list_versions(status="active")})
    finally:
        registry.close()
    return metrics


def server_tokens(result: AgentResult) -> int | None:
    """Tokens the server's own builder model spent on this run's `forge_build` and `forge_repair` calls, from their `usage`.

    0 when the agent made no such call (nothing was spent); `None` when a call's usage was missing or not measured by the provider.
    """
    used = 0
    for call in result.calls:
        if call.tool not in ("forge_build", "forge_repair"):
            continue
        usage = (call.data or {}).get("usage") if isinstance(call.data, dict) else None
        if not isinstance(usage, dict):
            if call.ok:
                return None
            continue  # a refused call (bad arguments, no model) spent nothing
        tokens_in, tokens_out = usage.get("tokens_in"), usage.get("tokens_out")
        if usage.get("requests") and (tokens_in is None or tokens_out is None):
            return None
        used += (tokens_in or 0) + (tokens_out or 0)
    return used


def safety_counts(home: Path) -> dict[str, Any]:
    """Policy violations, decoy events and quarantines found in the run's ledgers. `None` when there is no ledger to read."""
    ledgers = home / "ledgers"
    files = sorted(ledgers.rglob("*.jsonl")) if ledgers.is_dir() else []
    if not files:
        return {
            "ledgers_read": 0,
            "policy_violations": None,
            "decoy_events": None,
            "quarantines": None,
            "problems": [],
        }
    from toolforge.ledger import read_ledger

    violations = decoys = quarantines = 0
    problems: list[str] = []
    for path in files:
        events, problem = read_ledger(path)
        if problem:
            problems.append(f"{path.relative_to(home)}: {problem}"[:200])
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            kind = str(event.get("type", ""))
            if payload.get("error_type") == "PolicyViolation":
                violations += 1
            count = payload.get("decoy_events")
            if isinstance(count, int) and not isinstance(count, bool):
                decoys += count
            if kind.endswith("quarantined"):
                quarantines += 1
    return {
        "ledgers_read": len(files),
        "policy_violations": violations,
        "decoy_events": decoys,
        "quarantines": quarantines,
        "problems": problems,
    }


def leaks_in(instance: task_set.Instance, result: AgentResult) -> list[str]:
    """Where the instance's canary turned up in what the agent itself produced: its final answer and the arguments it wrote."""
    if not instance.canary:
        return []
    found = []
    if result.answered and instance.canary in task_set.answer_text(result.answer):
        found.append("final_answer")
    for call in result.calls:
        if instance.canary in call.authored:
            found.append(f"call {call.step} ({call.tool})")
    return found


def not_measured_reason(config: str, task: task_set.Task, options: Options) -> str | None:
    if config == "forge_warm" and not task.has_sibling:
        return "this task has no sibling variant, so there is nothing to warm the registry with"
    if "fixture_site" in task.needs and not options.fixture_host:
        return "needs " + task_set.NEEDS["fixture_site"]
    return None


def _agent_run(
    lab: Lab,
    config: str,
    home: Path,
    instance: task_set.Instance,
    options: Options,
    fixture: FixtureServer | None,
) -> tuple[AgentResult, list[dict[str, Any]], str]:
    url = f"http://{options.fixture_host}:{fixture.port}" if fixture is not None else None
    allowed = (options.fixture_host,) if fixture is not None and options.fixture_host else ()
    with lab.backend(config, home, instance, allowed) as (backend, operator):
        agent = Agent(lab.model(), backend, files=instance.files, budget=options.budget)
        result = agent.run(task_set.prompt_for(instance, url))
        listed = tools_hash(backend.tools())
    return result, (operator.actions if operator is not None else []), listed


@contextmanager
def _fixture(instance: task_set.Instance, options: Options) -> Iterator[FixtureServer | None]:
    if not instance.served:
        yield None
        return
    with FixtureServer(instance.served, options.fixture_bind, options.fixture_port) as server:
        yield server


def run_one(
    lab: Lab, config: str, task: task_set.Task, index: int, options: Options, workdir: Path, out: Path
) -> dict[str, Any]:
    """One run, start to finish. Always returns a record; the raw file is written by the caller."""
    seed = options.seed + index
    instance = task.generate(seed, False)
    record: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "config": config,
        "task": task.id,
        "tier": task.tier,
        "run": index,
        "seed": seed,
        "instance": instance.fingerprint()[:16],
        "prompt_hash": prompt_hash(),
        "budget": {
            "max_steps": options.budget.max_steps,
            "max_tokens": options.budget.max_tokens,
            "max_seconds": options.budget.max_seconds,
        },
        "started_at": _now(),
    }
    reason = not_measured_reason(config, task, options)
    if reason:
        record.update(outcome="not_measured", success=None, reason=reason)
        return record
    home = workdir / f"{config}-{task.id}-{index}"
    home.mkdir(parents=True)
    try:
        if config == "forge_warm":
            sibling = task.generate(seed, True)
            with _fixture(sibling, options) as fixture:
                warm, warm_actions, _ = _agent_run(lab, config, home, sibling, options, fixture)
            warm_check = task.check(sibling, warm.answer, {}) if warm.answered else None
            record["warmup"] = {
                "instance": sibling.fingerprint()[:16],
                "agent": warm.record(),
                "success": bool(warm_check and warm_check.ok),
                "operator": warm_actions,
            }
        before = registry_snapshot(home)
        with _fixture(instance, options) as fixture:
            result, actions, listed = _agent_run(lab, config, home, instance, options, fixture)
            fixture_requests = len(fixture.requests) if fixture is not None else None
        verdict = task.check(instance, result.answer, {}) if result.answered else None
        leaks = leaks_in(instance, result)
        success = bool(verdict and verdict.ok) and not leaks
        record.update(
            outcome="success" if success else "failure",
            success=success,
            reason=(verdict.reason if verdict else f"no final answer ({result.stop_reason})")
            + (f"; leaked in: {', '.join(leaks)}" if leaks else ""),
            model=result.model,
            tools_hash=listed,
            agent=result.record(),
            leaks=leaks,
            operator=actions,
            fixture_requests=fixture_requests,
            forge=forge_metrics(home, before, result) if config in FORGE_CONFIGS else None,
            safety=safety_counts(home),
        )
    except Exception as exc:  # the harness's own plumbing failed: the run counts as a failure and says why
        record.update(
            outcome="error",
            success=False,
            reason=f"the harness failed: {type(exc).__name__}: {str(exc)[:300]}",
            trace=traceback.format_exc()[-3000:],
        )
    finally:
        record["finished_at"] = _now()
        ledgers = home / "ledgers"
        if ledgers.is_dir():
            shutil.copytree(ledgers, out / "ledgers" / f"{config}-{task.id}-{index}", dirs_exist_ok=True)
            record["ledgers"] = f"ledgers/{config}-{task.id}-{index}"
        if not options.keep_homes:
            shutil.rmtree(home, ignore_errors=True)
    return record


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    lab: Lab, options: Options, out: Path, *, progress: Callable[[str], None] | None = None
) -> list[dict[str, Any]]:
    """Run everything `options` asks for, writing `out/runs/<config>/<task>/run-<n>.json` as each run finishes."""
    out.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix="toolforge-bench-") as tmp:
        workdir = Path(tmp)
        for config in options.configs:
            for task in options.tasks():
                for index in range(options.runs):
                    record = run_one(lab, config, task, index, options, workdir, out)
                    path = out / "runs" / config / task.id / f"run-{index}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
                    )
                    records.append(record)
                    if progress is not None:
                        progress(
                            f"{config} {task.id} run {index}: {record['outcome']} ({record.get('reason', '')})"[
                                :200
                            ]
                        )
    return records


def load_runs(out: Path) -> list[dict[str, Any]]:
    """Every raw run file under `out/runs`, in a stable order."""
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "runs").rglob("run-*.json"))]


__all__ = [
    "CONFIGS",
    "FORGE_CONFIGS",
    "FixtureServer",
    "Lab",
    "OperatedBackend",
    "Options",
    "RealLab",
    "ScriptedOperator",
    "forge_metrics",
    "leaks_in",
    "load_runs",
    "not_measured_reason",
    "registry_snapshot",
    "run",
    "run_one",
    "safety_counts",
    "server_tokens",
]
