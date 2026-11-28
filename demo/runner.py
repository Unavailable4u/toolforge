"""Runs the demo: the acts, in order, against one ToolForge home, with the agent scripted (replay) or a real model (live).

What is real in every mode: the MCP server, the registry, the gates, the sandbox, the ledgers, the Blast Radius report and the agent loop
(`bench.agent.Agent`, the one the benchmark uses). What is scripted in a replay: only the agent's replies (`demo/replay.py`). The runner
says which mode it is in before anything else, and says what it could not show (the decoy part of act 3 needs a real sandbox).

Two sandboxes: `docker` (the default; hardened containers through MiniLocker, the server a child process over stdio) and `stand-in`
(`demo/standin.py`: no isolation, for development and for tests, refuses hostile fixtures, and never for a live model).

The checker that judges each act's answer is the benchmark's hidden one. It is used here only to report whether the answer was right; the
agent never sees it.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from bench.agent import Agent, Budget, McpBackend, ToolBackend, ToolReply, ToolSpec
from bench.harness import OperatedBackend
from bench.tasks import prompt_for
from demo import acts as act_list
from demo import hostile, recording
from demo.operator_script import DemoOperator
from demo.replay import ReplayError, ReplayModel, TaskRef, TurnRecorder, load_transcripts
from toolforge import __version__
from toolforge.textsafety import sanitize

ROOT = (
    Path(__file__).resolve().parent.parent
)  # the child server starts here, so it reads the same .env this process does
SANDBOXES = ("docker", "stand-in")
MAX_STEPS = 12
GATE_NAMES: dict[str, str] = {}


class DemoError(Exception):
    """Something that stops the demo before or between acts, with advice."""


@dataclass
class Options:
    mode: str = "replay"  # replay | live
    transcripts: Path | None = None
    sandbox: str = "docker"
    acts: list[act_list.Act] = field(default_factory=lambda: list(act_list.ACTS))
    out: Path = Path("runs/demo")
    record: Path | None = None
    seed: int = act_list.SEED
    pace_s: float = 0.0
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)

    @property
    def isolated(self) -> bool:
        return self.sandbox == "docker"


class Screen:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self._lock = threading.Lock()

    def line(self, text: str = "") -> None:
        with self._lock:
            print(text, file=self.stream, flush=True)

    def head(self, text: str) -> None:
        self.line()
        self.line(f"== {text} " + "=" * max(4, 72 - len(text)))


# --- Live display of the ledger -----------------------------------------------------------------------------------------------


def _gate_name(gate: str) -> str:
    if not GATE_NAMES:
        from toolforge.verify.pipeline import GATE_INFO

        GATE_NAMES.update({g.id: g.name for g in GATE_INFO})
    return GATE_NAMES.get(gate, "")


def describe(event: dict[str, Any]) -> str | None:
    """One screen line for a ledger event worth showing, or None. Text from the ledger goes through `sanitize` and a length cap."""
    kind, payload = str(event.get("type", "")), event.get("payload") or {}
    tool = sanitize(str(payload.get("tool", "")), max_chars=60)
    if kind in ("forge.gate.passed", "forge.gate.failed", "forge.gate.skipped"):
        gate = str(payload.get("gate", "?"))
        status = kind.rsplit(".", 1)[1]
        took = f" ({payload['duration_ms']} ms)" if isinstance(payload.get("duration_ms"), int) else ""
        summary = sanitize(str(payload.get("summary", "")), max_chars=100).replace("...[truncated]", "...")
        return f"    {gate} {_gate_name(gate):<12} {status.upper():<8}{took}  {summary}"
    if kind in ("forge.verified", "forge.rejected"):
        return f"    -> {tool or payload.get('tool', '')} {kind.split('.')[1].upper()}"
    if kind == "forge.promoted":
        return f"    -> {tool} PROMOTED by the operator"
    if kind == "forge.quarantined":
        return f"    -> {tool} QUARANTINED"
    if kind.startswith("canary.") or (kind.startswith("forge.") and payload.get("decoy_events")):
        return f"    !! decoy event in the ledger: {kind}"
    if kind in ("forge.session.opened", "forge.session.rewound", "forge.session.closed"):
        extra = ""
        if kind.endswith("rewound"):
            extra = f" (step {payload.get('from_step')} -> {payload.get('to_step')})"
        return f"    session {kind.rsplit('.', 1)[1]}{extra}"
    return None


class LedgerTail:
    """Prints new ledger events as the files grow. Reads complete lines only; never writes."""

    def __init__(self, home: Path, screen: Screen, pace_s: float = 0.0) -> None:
        self.home, self.screen, self.pace_s = home, screen, pace_s
        self._offsets: dict[Path, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._lock = threading.Lock()

    def poll(self) -> None:
        with self._lock:
            ledgers = self.home / "ledgers"
            if not ledgers.is_dir():
                return
            found = []
            for path in ledgers.rglob("*.jsonl"):
                found.append(path)
            for path in sorted(found):
                try:
                    with path.open("rb") as handle:
                        handle.seek(self._offsets.get(path, 0))
                        chunk = handle.read()
                except OSError:
                    continue
                cut = chunk.rfind(b"\n") + 1
                self._offsets[path] = self._offsets.get(path, 0) + cut
                for raw in chunk[:cut].splitlines():
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    text = describe(event) if isinstance(event, dict) else None
                    if text:
                        self.screen.line(text)
                        if self.pace_s:
                            time.sleep(self.pace_s)

    def _loop(self) -> None:
        while not self._stop.wait(0.05):
            self.poll()

    def __enter__(self) -> LedgerTail:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.poll()


# --- Backends -----------------------------------------------------------------------------------------------------------------


class _Narrating:
    """Wraps a backend to show each tool call and its outcome, with an optional pause between steps (for screen recording)."""

    def __init__(self, inner: ToolBackend, screen: Screen, pace_s: float) -> None:
        self.inner, self.screen, self.pace_s = inner, screen, pace_s
        self.files_note = inner.files_note

    def tools(self) -> list[ToolSpec]:
        return self.inner.tools()

    def call(self, name: str, arguments: dict[str, Any]) -> ToolReply:
        self.screen.line(f"  agent calls {name}")
        reply = self.inner.call(name, arguments)
        self.screen.line(f"  {name} {'returned' if not reply.is_error else 'FAILED'}")
        if self.pace_s:
            time.sleep(self.pace_s)
        return reply


def _check_docker() -> None:
    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:
        raise DemoError(
            f"no Docker daemon answered ({type(exc).__name__}). Start Docker and build the runtime image with `toolforge images build`, "
            "or use --sandbox stand-in for a development run without isolation (it cannot show the decoy part of act 3)."
        ) from exc


@contextmanager
def open_backend(
    options: Options, home: Path, operator_hook: Callable[[dict[str, Any]], None]
) -> Iterator[tuple[ToolBackend, DemoOperator]]:
    from toolforge.config import load_settings

    refuse = hostile.NAMES
    if options.sandbox == "stand-in":
        from demo.standin import guarded_runner
        from toolforge import cli_run
        from toolforge.config import Settings
        from toolforge.registry import Registry
        from toolforge.server.app import build_server

        settings = Settings(home=home)
        runner = guarded_runner()
        try:
            with Registry(settings.home) as registry:
                app = build_server(
                    settings, registry=registry, sandbox=runner, ledger_factory=cli_run.make_ledger
                )
                operator = DemoOperator(settings, refuse, operator_hook)
                try:
                    with McpBackend(app.server) as backend:
                        yield OperatedBackend(backend, operator), operator  # type: ignore[arg-type]
                finally:
                    app.close()  # stops the session reaper before the registry closes, as `toolforge serve` does
        finally:
            runner.cleanup()
        return
    _check_docker()
    import os

    from mcp import StdioServerParameters

    settings = load_settings().model_copy(update={"home": home})
    env = dict(os.environ)
    env["TOOLFORGE_HOME"] = str(home)
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "toolforge.cli", "serve"], env=env, cwd=str(home)
    )
    operator = DemoOperator(settings, refuse, operator_hook)
    with McpBackend(server) as backend:
        yield OperatedBackend(backend, operator), operator  # type: ignore[arg-type]


# --- One act ------------------------------------------------------------------------------------------------------------------


def run_act(
    act: act_list.Act,
    options: Options,
    backend: ToolBackend,
    screen: Screen,
    transcripts: dict[str, Any],
    settings_llm: Any,
    tail: LedgerTail,
) -> dict[str, Any]:
    task = act_list.task_for(act)
    instance = task.generate(options.seed, act.sibling)
    screen.head(f"Act {act.number}: {act.title}")
    screen.line(act.shows)
    left_out = 0
    recorder: TurnRecorder | None = None
    if options.mode == "replay":
        transcript = transcripts.get(act.id)
        if transcript is None:
            raise DemoError(f"no transcript for act {act.id!r}; run `python demo/make_transcripts.py`")
        model: Any = ReplayModel(transcript, isolated=options.isolated)
        left_out = model.skipped
    else:
        recorder = TurnRecorder(settings_llm)
        model = recorder
    screen.line(
        "Task: " + sanitize(instance.prompt.splitlines()[0], max_chars=110).replace("...[truncated]", " ...")
    )
    if left_out:
        screen.line(
            f"  ({left_out} scripted turn left out: it needs a real sandbox, and this run uses the stand-in. The decoy part of this act is NOT shown.)"
        )
    agent = Agent(
        model,
        _Narrating(backend, screen, options.pace_s),
        files=instance.files,
        budget=Budget(max_steps=MAX_STEPS),
    )
    started = time.monotonic()
    try:
        result = agent.run(prompt_for(instance, None))
    except ReplayError as exc:
        raise DemoError(str(exc)) from exc
    tail.poll()
    verdict = task.check(instance, result.answer, {}) if result.answered else None
    if isinstance(model, ReplayModel) and not model.finished and result.stop_reason == "answered":
        raise DemoError(f"act {act.id}: the agent answered before its script ended")
    ok = bool(verdict and verdict.ok)
    screen.line(
        f"Answer: {'ACCEPTED' if ok else 'REJECTED'} by the hidden checker ({verdict.reason if verdict else result.stop_reason})"
    )
    screen.line(
        f"Agent: {result.steps} tool calls, {result.requests} model requests, about {result.tokens_estimated} tokens "
        f"(estimated, 4 characters a token; the replay reports none), {int(time.monotonic() - started)} s"
    )
    if recorder is not None:
        saved = recorder.transcript(act.id, TaskRef(id=act.task, seed=options.seed, sibling=act.sibling))
        if saved is not None:
            target = options.out / "transcripts"
            target.mkdir(parents=True, exist_ok=True)
            (target / act.transcript).write_text(saved.to_json(), encoding="utf-8")
    record = result.record()
    return {
        "act": act.id,
        "title": act.title,
        "task": act.task,
        "sibling": act.sibling,
        "stop_reason": result.stop_reason,
        "answered": result.answered,
        "checker": "accepted" if ok else "rejected",
        "checker_reason": verdict.reason if verdict else result.stop_reason,
        "steps": result.steps,
        "requests": result.requests,
        "tokens_estimated": result.tokens_estimated,
        "tokens_measured": result.tokens,
        "wall_ms": record["wall_ms"],
        "turns_left_out": left_out,
        "calls": record["calls"],
        "ok": ok,
    }


# --- After the acts: what the audience should look at ---------------------------------------------------------------------------


def show_attack(settings: Any, screen: Screen) -> None:
    from toolforge.registry import Registry
    from toolforge.report import blast_radius

    screen.line("What the registry and the ledger say about the hostile packages:")
    shown = None
    with Registry(settings.home) as registry:
        for name in hostile.NAMES:
            for record in registry.list_versions(name=name):
                failed = [g for g in registry.gate_results(record.id) if g.status == "failed"]
                why = (
                    sanitize(str((failed[0].detail or {}).get("summary", "")), max_chars=110)
                    if failed
                    else ""
                )
                where = f"{failed[0].gate} failed: {why}" if failed else "no gate failed"
                screen.line(f"  {record.label}: {record.status.upper()} ({where})")
                shown = record
        if shown is None:
            screen.line("  (no hostile version is in the registry)")
            return
    try:
        document = blast_radius.generate(settings, shown.name, shown.version, store=False)
    except (
        Exception
    ) as exc:  # a version that never ran has no report to build: say so rather than skip the line
        document = blast_radius.missing_summary(f"no report could be built ({type(exc).__name__})")
        document.update({"tool": shown.name, "version": shown.version})
    screen.line("")
    for line in blast_radius.render_text(document).splitlines():
        screen.line("  " + sanitize(line, max_chars=160).replace("...[truncated]", "..."))


def show_comparison(outcomes: list[dict[str, Any]], screen: Screen) -> None:
    by_id = {o["act"]: o for o in outcomes}
    if "forge" not in by_id or "reuse" not in by_id:
        return
    screen.head("Act 1 next to act 2")
    screen.line(f"{'':<8}{'tool calls':>12}{'requests':>10}{'tokens (est.)':>15}{'tool built':>12}")
    for key in ("forge", "reuse"):
        o = by_id[key]
        built = sum(1 for c in o["calls"] if c["tool"] == "forge_submit")
        screen.line(
            f"{key:<8}{o['steps']:>12}{o['requests']:>10}{o['tokens_estimated']:>15}{('yes' if built else 'no'):>12}"
        )


# --- The whole run --------------------------------------------------------------------------------------------------------------


def run(options: Options) -> tuple[int, dict[str, Any]]:
    from toolforge.config import Settings

    screen = Screen(options.stream)
    if options.mode == "live" and options.sandbox != "docker":
        raise DemoError(
            "--live needs --sandbox docker: a real model may write hostile code, and the stand-in has no isolation"
        )
    transcripts: dict[str, Any] = {}
    if options.mode == "replay":
        if options.transcripts is None:
            raise DemoError("replay needs a transcripts directory: --replay demo/transcripts/")
        transcripts = load_transcripts(options.transcripts)
    if options.isolated:
        _check_docker()  # before the banner and before any folder is made: the advice must not follow a claim that Docker is in use
    stamp = options.now().strftime("%Y%m%dT%H%M%SZ")
    options.out = options.out / stamp
    home = options.out / "home"
    home.mkdir(parents=True)
    llm = None
    if options.mode == "live":
        from toolforge.config import load_settings
        from toolforge.llm.client import client_from_settings

        llm = client_from_settings(load_settings(), "builder")
        if llm is None:
            raise DemoError(
                "--live needs a model: set TOOLFORGE_LLM_BASE_URL and TOOLFORGE_LLM_MODEL (docs/guide/04-optional-resources.md)"
            )
    screen.head("ToolForge demo")
    if options.mode == "replay":
        screen.line(
            "REPLAY: the agent's replies are scripted (demo/transcripts/); no language model is involved."
        )
    else:
        screen.line(f"LIVE: the agent is the model {getattr(llm, 'model', '?')}.")
    screen.line("The MCP server, registry, gates, sandbox and ledger are real in both modes.")
    if options.isolated:
        screen.line("Sandbox: Docker (hardened containers through MiniLocker).")
    else:
        screen.line(
            "Sandbox: STAND-IN. No isolation: development only. It refuses the hostile fixtures, so the decoy part of act 3 is not run."
        )
    screen.line(f"Seed {options.seed}. Results go to {options.out}.")
    screen.line(f"ToolForge home for this run (point `toolforge dashboard` at it): {home}")

    outcomes: list[dict[str, Any]] = []
    operator_actions: list[dict[str, Any]] = []
    windows: dict[str, tuple[float, float]] = {}

    tail = LedgerTail(home, screen, options.pace_s)

    def on_action(action: dict[str, Any]) -> None:
        tail.poll()
        operator_actions.append(action)
        verb = {
            "promote": "PROMOTES",
            "refuse": "REFUSES to promote",
            "deny": "DENIES",
            "leave_pending": "leaves pending",
        }.get(action["action"], action["action"])
        screen.line(
            f"    operator (a person, not the agent) {verb} {action.get('version') or action.get('approval', '')}"
        )

    settings = Settings(home=home)
    with tail, open_backend(options, home, on_action) as (backend, _operator):
        for act in options.acts:
            begin = time.time()
            outcome = run_act(act, options, backend, screen, transcripts, llm, tail)
            windows[act.id] = (begin, time.time())
            outcomes.append(outcome)
            if act.id == "attack":
                show_attack(settings, screen)
    show_comparison(outcomes, screen)
    ok = all(o["ok"] for o in outcomes)
    summary = {
        "schema": 1,
        "toolforge_version": __version__,
        "mode": options.mode,
        "sandbox": options.sandbox,
        "isolated": options.isolated,
        "seed": options.seed,
        "acts": [{k: v for k, v in o.items() if k != "calls"} for o in outcomes],
        "operator": operator_actions,
    }
    (options.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    ledgers = home / "ledgers"
    if ledgers.is_dir():
        shutil.copytree(ledgers, options.out / "ledgers", dirs_exist_ok=True)
    if options.record is not None:
        write_record(options, home, outcomes, windows, screen)
    screen.line()
    screen.line("Every act passed." if ok else "At least one act did not pass: see above.")
    return (0 if ok else 1), summary


def write_record(
    options: Options,
    home: Path,
    outcomes: list[dict[str, Any]],
    windows: dict[str, tuple[float, float]],
    screen: Screen,
) -> None:
    from toolforge.ledger import read_ledger, verify_chain

    files = []
    verified = 0
    ledgers = home / "ledgers"
    for path in sorted(ledgers.rglob("*.jsonl")) if ledgers.is_dir() else []:
        events, problem = read_ledger(path)
        if problem is None and verify_chain(events)[0]:
            verified += 1
        label = path.parent.name if path.parent.name != "sessions" else path.parent.parent.name
        files.append((label, recording.kind_of_file(path), events))
    events, dropped = recording.sanitise(files, windows)
    g5 = {
        e["type"].rsplit(".", 1)[1]
        for e in events
        if e["facts"].get("gate") == "G5" and e["type"] != "forge.gate.started"
    }
    meta = {
        "recorded_at": options.now().strftime("%Y-%m-%d"),
        "toolforge_version": __version__,
        "seed": options.seed,
        "mode": options.mode,
        "agent": "scripted" if options.mode == "replay" else "model",
        "sandbox": options.sandbox,
        "isolated": options.isolated,
        "g5": "ran"
        if g5 == {"passed"}
        else "skipped"
        if g5 == {"skipped"}
        else "mixed"
        if g5
        else "not reached",
        "acts": [{"id": o["act"], "checker": o["checker"]} for o in outcomes],
        "events": len(events),
        "events_dropped": dropped,
        "ledger_files": len(files),
        "ledger_files_verified": verified,
        "note": ("Replayed with a scripted agent." if options.mode == "replay" else "Live agent.")
        + (
            ""
            if options.isolated
            else " The stand-in sandbox gives no isolation, so this recording has no G6 decoy run and no Docker facts."
        ),
    }
    write_recording(options.record, meta, events, outcomes, screen)


def write_recording(
    target: Path,
    meta: dict[str, Any],
    events: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    screen: Screen,
) -> None:
    paths = recording.write_recording(target, meta, events, recording.transcript_shape(outcomes))
    problems = recording.validate(target)
    for path in paths:
        screen.line(f"recorded {path}")
    if problems:
        raise DemoError(
            "the recording did not validate and should not be committed: " + "; ".join(problems[:3])
        )
