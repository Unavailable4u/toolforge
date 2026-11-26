"""Declarative tool chains (S02, optional): a short fixed list of verified tools, run in order in one call.

A chain is **data**. Each step names an active tool and gives its arguments; a value in the arguments is either a literal JSON value or an
object with exactly one key, `$ref`, whose text points into the output of an *earlier* step (`<step id>.output` and then `.key` or `.0`
parts). That is the whole language: no expressions, no templates, no conditions, no loops, no functions. Nothing a tool returns can change
what the server does next, because the server never reads a value except to copy it into the arguments of a later step.

Security properties, and the tests that prove them (`tests/test_chain.py` unless named):

* A chain cannot become a way around approvals. A tool is chainable only if its active version has no network hosts, no secrets, no file
  inputs and no session, is `active`, and has the stored blob and the passed build gate (G2) that `promote` requires. Every step is checked
  **before step 1 runs**, and a refusal names the step and the reason (`test_a_tool_that_is_not_eligible_stops_the_chain_before_step_one`).
* Every step is an ordinary `ToolRunner.run` and nothing else: its own sandbox, limits, ledger events, statistics, argument check and
  decoy/secret tripwires. No shared filesystem and no shared process: only a copied JSON value crosses, through this module
  (`test_a_chain_calls_only_the_runner`, which lists every call the module makes).
* The versions are pinned when the chain starts. A promote in the middle changes nothing that runs; a version that was quarantined in the
  middle stops the chain at that step (`test_versions_are_pinned_for_the_whole_chain`).
* A reference is read only in the request's `args`, never in an output. It is resolved once, before its step runs, by copying a JSON value
  (at most 64 KiB) out of the recorded output of an earlier step; a missing path is an error that names the step, never `null`. What was
  copied is never read again for `$ref` (`test_a_ref_inside_an_output_is_data_and_is_never_resolved`).
* Output of a tool is hostile text. This module never puts it in a prompt, a description or a `$ref` position, and never writes it to a
  ledger: the chain's ledger has step ids, tool labels, statuses, run ids, hashes and sizes (`test_the_chain_ledger_holds_no_output`).
  Fencing for the calling model is done by `server.tools.chain_view` (`tests/test_chain_server.py`).
* This module imports nothing that can approve, promote, submit, repair or build (`test_a_chain_calls_only_the_runner`).

What a chain does not do: it has no condition, no loop and no way to call a tool from inside a tool, and it cannot be saved as a composite
tool. The wall-clock budget (`chain_total_seconds`) decides only whether another step may *start*; a step that is already running keeps its
own limit and is not interrupted.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from toolforge import textsafety
from toolforge.config import Settings
from toolforge.errors import LedgerError, RegistryError, ToolNotRunnable, ValidationError
from toolforge.models.package import json_problem
from toolforge.models.results import CallError
from toolforge.registry import VersionRecord, lifecycle

log = logging.getLogger(__name__)

SUBMITTER = "agent"  # MCP carries no caller identity; every other tool of the server says "agent" too
HARD_MAX_STEPS = 8  # `chain_max_steps` may not be set above this
STEP_ID = r"[a-z][a-z0-9_]{0,15}"
REF_KEY = "$ref"
KEY_PART = re.compile(
    r"[A-Za-z][A-Za-z0-9_]{0,63}"
)  # a letter first, so `__proto__`, `_x` and `0` are never keys
INDEX_PART = re.compile(r"0|[1-9][0-9]{0,5}")  # no sign, no leading zero
MAX_REF_CHARS = 200
MAX_REF_PARTS = 16
MAX_REFS_PER_STEP = 32
MAX_REQUEST_BYTES = 256 * 1024
MAX_COPY_BYTES = 64 * 1024  # one `$ref` copies at most this much JSON
MAX_TOOL_NAME_CHARS = 120
STEP_FIELDS = ("id", "tool", "args")
STEP_STATUSES = ("ok", "failed", "skipped")
STOP_REASONS = ("step_failed", "time_budget")
# A step may run a version that was active when the chain began and was deprecated meanwhile (a promote in the middle). It may not run
# anything else: a quarantined version stops the chain.
RUNNABLE_STATUSES = (lifecycle.ACTIVE, lifecycle.DEPRECATED)
# A call that was refused before anything ran: it has a call id but no ledger and no sandbox.
NOT_RUN_ERRORS = frozenset({"InvalidArguments", "InvalidFiles"})
CHAINS_DIR = "chains"


def _shown(value: object, limit: int = 40) -> str:
    """A caller-chosen string made safe to put in a message (`sanitize` needs room for its own cut mark, so a small limit is raised)."""
    text = value if isinstance(value, str) else str(value)
    return repr(textsafety.sanitize(text, max_chars=max(limit, textsafety.MIN_MAX_CHARS)))


def _bad(message: str, hint: str = "") -> ValidationError:
    return ValidationError(message, code="bad_chain", hint=hint or None)


# --- The reference grammar -------------------------------------------------------------------------------------------------------


class RefError(ValueError):
    """A `$ref` text that is not in the grammar. The message says what is wrong and never echoes more than a short, cleaned piece of it."""


@dataclass(frozen=True)
class Ref:
    step: str
    parts: tuple[str | int, ...]
    text: str

    @property
    def path(self) -> str:
        return ".".join([self.step, "output", *(str(part) for part in self.parts)])


def parse_ref(text: object) -> Ref:
    """`<step id>.output` followed by zero or more `.key` or `.index` parts. Raises `RefError`.

    A key is a letter and then letters, digits or `_` (so `__proto__` and a key that is only digits can never be named); an index is a
    plain non-negative integer. There is no other syntax: no brackets, no quotes, no `..`, no leading `/`, no wildcard, no sign.
    """
    if not isinstance(text, str):
        raise RefError("a `$ref` must be a string like `a.output.rows`")
    if len(text) > MAX_REF_CHARS:
        raise RefError(f"a `$ref` is at most {MAX_REF_CHARS} characters")
    segments = text.split(".")
    if len(segments) < 2 or not re.fullmatch(STEP_ID, segments[0]) or segments[1] != "output":
        raise RefError(
            f"{_shown(text)} must start with `<step id>.output`, for example `a.output.rows`",
        )
    parts: list[str | int] = []
    for segment in segments[2:]:
        if INDEX_PART.fullmatch(segment):
            parts.append(int(segment))
        elif KEY_PART.fullmatch(segment):
            parts.append(segment)
        else:
            raise RefError(
                f"{_shown(segment)} in a `$ref` is neither a key (a letter, then letters, digits or `_`) nor an index (0, 1, 2, ...)"
            )
    if len(parts) > MAX_REF_PARTS:
        raise RefError(f"a `$ref` has at most {MAX_REF_PARTS} parts after `output`")
    return Ref(segments[0], tuple(parts), text)


# --- Parsing a request -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    id: str
    tool: str  # a tool name; the active version is resolved when the chain starts
    args: dict[str, Any]  # plain JSON that may hold `{"$ref": "..."}` objects; never changed after parsing
    refs: int


def _scan(value: Any, where: str, step: str, earlier: frozenset[str]) -> int:
    """Check one value of a step's `args` and count the references in it. Raises `ValidationError`."""
    if isinstance(value, dict):
        if REF_KEY in value:
            if len(value) != 1:
                raise _bad(
                    f"step `{step}`, {where}: an object with a `$ref` key may have no other key",
                    'write {"$ref": "a.output.rows"} and nothing else in that object; there is no way to combine a reference with other fields',
                )
            try:
                ref = parse_ref(value[REF_KEY])
            except RefError as exc:
                raise _bad(f"step `{step}`, {where}: {exc}") from None
            if ref.step not in earlier:
                raise _bad(
                    f"step `{step}`, {where}: `$ref` names step `{ref.step}`, which is not an earlier step of this chain",
                    "a step can read only the output of a step that comes before it",
                )
            return 1
        return sum(
            _scan(child, f"{where}.{textsafety.sanitize(key, max_chars=40)}", step, earlier)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return sum(_scan(child, f"{where}[{index}]", step, earlier) for index, child in enumerate(value))
    return 0


def parse_request(raw: object, *, max_steps: int) -> list[Step]:
    """The steps of a chain request, checked. Raises `ValidationError` (`bad_chain`) before anything has run."""
    if not isinstance(raw, list):
        raise _bad(
            "steps must be a list of {id, tool, args} objects",
            'for example [{"id": "a", "tool": "word_count", "args": {...}}]',
        )
    if not 1 <= len(raw) <= max_steps:
        raise _bad(
            f"a chain has 1 to {max_steps} steps; this one has {len(raw)}",
            f"split it into shorter chains; the operator limits a chain to {max_steps} steps",
        )
    problem = json_problem(raw)
    if problem is not None:
        raise _bad(f"the request is not plain JSON data: {problem[1]}")
    if len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise _bad(
            f"the request is larger than {MAX_REQUEST_BYTES // 1024} KB",
            "pass big values between steps with `$ref`, not inside the request",
        )
    steps: list[Step] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise _bad(f"step {position} must be an object with id, tool and args")
        extra = [key for key in item if key not in STEP_FIELDS]
        if extra:
            shown = ", ".join(_shown(key, 20) for key in extra[:3])
            raise _bad(
                f"step {position} has a field that a step does not have: {shown}",
                "a step has only `id`, `tool` and `args`: a chain is data, not a program, so there is no script, condition or loop",
            )
        step_id = item.get("id")
        if not isinstance(step_id, str) or not re.fullmatch(STEP_ID, step_id):
            raise _bad(
                f"step {position}: `id` must match {STEP_ID} (a lower-case letter, then at most 15 letters, digits or `_`)"
            )
        if step_id in seen:
            raise _bad(f"step {position}: the id `{step_id}` is used twice", "give every step its own id")
        tool = item.get("tool")
        if (
            not isinstance(tool, str)
            or not tool.strip()
            or len(tool) > MAX_TOOL_NAME_CHARS
            or not re.fullmatch(r"[a-z][a-z0-9_]*", tool)
        ):
            raise _bad(
                f"step `{step_id}`: `tool` must be a tool name such as `word_count`",
                "name the tool without a version: a chain runs the active version. forge_find lists tools",
            )
        args = item.get("args", {})
        if not isinstance(args, dict):
            raise _bad(f"step `{step_id}`: `args` must be an object")
        count = _scan(args, "args", step_id, frozenset(seen))
        if count > MAX_REFS_PER_STEP:
            raise _bad(
                f"step `{step_id}` has {count} references; at most {MAX_REFS_PER_STEP} are allowed in one step"
            )
        steps.append(Step(step_id, tool, copy.deepcopy(args), count))
        seen.add(step_id)
    return steps


# --- Resolving a reference -------------------------------------------------------------------------------------------------------


class _StepProblem(Exception):
    """A reference that cannot be resolved for this step. Its message is written here, from the reference's own text only."""


def _kind(value: Any) -> str:
    return {dict: "an object", list: "a list", str: "text", bool: "true or false", type(None): "null"}.get(
        type(value), "a number"
    )


def _lookup(ref: Ref, outputs: Mapping[str, Any], where: str) -> Any:
    """A copy of the value `ref` points at, in the recorded output of an earlier step. Raises `_StepProblem`."""
    current = outputs[ref.step]
    walked = f"{ref.step}.output"
    for part in ref.parts:
        if isinstance(part, int):
            if not isinstance(current, list):
                raise _StepProblem(
                    f"{where}: `{ref.path}` does not exist: `{walked}` is {_kind(current)}, not a list"
                )
            if part >= len(current):
                raise _StepProblem(
                    f"{where}: `{ref.path}` does not exist: `{walked}` has {len(current)} items, so there is no item {part}"
                )
            current = current[part]
        else:
            if not isinstance(current, dict):
                raise _StepProblem(
                    f"{where}: `{ref.path}` does not exist: `{walked}` is {_kind(current)}, not an object"
                )
            if part not in current:
                raise _StepProblem(f"{where}: `{ref.path}` does not exist: `{walked}` has no key `{part}`")
            current = current[part]
        walked = f"{walked}.{part}"
    try:
        size = len(
            json.dumps(current, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        )
        if size > MAX_COPY_BYTES:
            raise _StepProblem(
                f"{where}: `{ref.path}` is {size // 1024} KB, and a reference copies at most {MAX_COPY_BYTES // 1024} KB"
            )
        return copy.deepcopy(current)
    except (TypeError, ValueError, RecursionError):
        raise _StepProblem(f"{where}: `{ref.path}` is not usable JSON") from None


def substitute(value: Any, outputs: Mapping[str, Any], where: str = "args") -> Any:
    """`value` with every `{"$ref": ...}` replaced by a copy. Only the request's own template is walked: what a copy holds is not."""
    if isinstance(value, dict):
        if REF_KEY in value:
            return _lookup(parse_ref(value[REF_KEY]), outputs, where)
        return {key: substitute(child, outputs, f"{where}.{key}") for key, child in value.items()}
    if isinstance(value, list):
        return [substitute(child, outputs, f"{where}[{index}]") for index, child in enumerate(value)]
    return value


# --- Results ---------------------------------------------------------------------------------------------------------------------


@dataclass
class StepResult:
    """One step as the caller sees it. `output` is the tool's own JSON and is hostile text: `server.tools.chain_view` fences it."""

    id: str
    tool: str  # name@version, as pinned
    status: str  # ok, failed or skipped
    run_id: str | None = None  # the call id of the step's `forge.call.*` events; None when nothing ran
    duration_ms: int | None = None
    output: Any = None
    error: CallError | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    truncated: bool = False
    timed_out: bool = False


@dataclass
class ChainResult:
    chain_id: str
    steps: list[StepResult] = field(default_factory=list)
    stop_reason: str | None = None  # None when every step is ok
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.stop_reason is None and all(step.status == "ok" for step in self.steps)


def _digest(value: Any) -> tuple[str, int]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str).encode("utf-8")
    return hashlib.sha256(text).hexdigest(), len(text)


# --- The chain runner ------------------------------------------------------------------------------------------------------------


class ChainRunner:
    """Runs chains. `runner` is a `ToolRunner` (or anything with the same `run`), `ledger_factory(path, task_id)` makes a ledger."""

    def __init__(
        self,
        settings: Settings,
        registry: Any,
        runner: Any,
        *,
        ledger_factory: Callable[[str | None, str], Any],
        clock: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._ledger_factory = ledger_factory
        self._clock = clock
        self._new_id = new_id or (lambda: uuid.uuid4().hex[:16])

    # -- the request -----------------------------------------------------------------------------------------------------------

    def run(self, raw_steps: object) -> ChainResult:
        """Run a chain. Raises `ToolNotRunnable` (`chain_disabled`, `chain_not_eligible`) or `ValidationError` before any step runs."""
        settings = self._settings
        if not settings.chain_enabled:
            raise ToolNotRunnable(
                "chains are switched off on this server",
                code="chain_disabled",
                hint="call the tools one by one with forge_run, or ask the operator to set TOOLFORGE_CHAIN_ENABLED=true",
            )
        steps = parse_request(raw_steps, max_steps=min(settings.chain_max_steps, HARD_MAX_STEPS))
        pinned = self._pin(steps)
        chain_id = self._new_id()
        path = str(Path(settings.home) / "ledgers" / CHAINS_DIR / f"{chain_id}.jsonl")
        try:  # before anything runs: no chain without a record, as with a call
            self._ledger_factory(path, chain_id).append(
                "forge",
                "forge.chain.started",
                {
                    "chain_id": chain_id,
                    "caller": SUBMITTER,
                    "max_steps": min(settings.chain_max_steps, HARD_MAX_STEPS),
                    "total_seconds": settings.chain_total_seconds,
                    "steps": [
                        {
                            "id": step.id,
                            "tool": step.tool,
                            "version": pinned[step.tool].version,
                            "code_sha": pinned[step.tool].code_sha,
                            "refs": step.refs,
                        }
                        for step in steps
                    ],
                },
            )
        except Exception as exc:
            raise LedgerError(
                "the chain's history could not be written, so nothing ran",
                hint=f"{type(exc).__name__}: check the data directory",
            ) from exc
        return self._run_steps(chain_id, path, steps, pinned)

    # -- eligibility -----------------------------------------------------------------------------------------------------------

    def _pin(self, steps: list[Step]) -> dict[str, VersionRecord]:
        """The version each tool name means for this whole chain, or a refusal naming every step that cannot be chained."""
        pinned: dict[str, VersionRecord] = {}
        problems: list[str] = []
        for name in dict.fromkeys(step.tool for step in steps):
            ids = [step.id for step in steps if step.tool == name]
            who = f"step `{ids[0]}`" if len(ids) == 1 else "steps " + ", ".join(f"`{i}`" for i in ids)
            record = self._registry.active_version(name)
            reason = self._not_active(name) if record is None else self._ineligible(record)
            if reason is not None:
                problems.append(f"{who} (tool `{name}`): {reason}")
            else:
                assert record is not None
                pinned[name] = record
        if problems:
            raise ToolNotRunnable(
                "the chain was refused before any step ran: " + "; ".join(problems),
                code="chain_not_eligible",
                hint="a chain runs only active tools that have no network, no secrets, no session and no file inputs; use forge_run for the others",
            )
        return pinned

    def _not_active(self, name: str) -> str:
        versions = self._registry.list_versions(name)
        if not versions:
            return "there is no such tool; forge_find lists the tools"
        newest = versions[-1]
        return (
            f"it has no active version (its newest, {newest.version}, is {newest.status.replace('_', ' ')})"
        )

    def _ineligible(self, record: VersionRecord) -> str | None:
        """Why this active version may not be chained, or None. The same build check as `promote` (a passed G2 and a stored blob)."""
        manifest = record.manifest
        perms = manifest.get("permissions") if isinstance(manifest.get("permissions"), dict) else {}
        kind = manifest.get("kind", "stateless")
        if kind == "session":
            return "it keeps a session, and a chain runs stateless tools only"
        if kind != "stateless":
            return f"it is a {textsafety.sanitize(str(kind), max_chars=40)} tool, and a chain runs stateless tools only"
        if perms.get("network"):
            return "it asks for network hosts, and no step of a chain may have network access"
        if perms.get("secrets"):
            return "it asks for secrets, and no step of a chain may use a credential"
        if perms.get("inputs"):
            return "it takes files, and a chain starts from JSON only"
        if perms.get("browser"):
            return "it uses a browser, and a chain runs plain tools only"
        builds = [gate for gate in self._registry.gate_results(record.id) if gate.gate == "G2"]
        if not builds or builds[-1].status != "passed":
            return "its build gate (G2) has not passed"
        if not record.blob_sha or not self._registry.blobs.exists(record.blob_sha):
            return "its frozen package is not in the store"
        return None

    # -- running ---------------------------------------------------------------------------------------------------------------

    def _run_steps(
        self, chain_id: str, path: str, steps: list[Step], pinned: dict[str, VersionRecord]
    ) -> ChainResult:
        budget_s = self._settings.chain_total_seconds
        began = self._clock()
        result = ChainResult(chain_id)
        outputs: dict[str, Any] = {}
        finished: dict[str, Any] = {"chain_id": chain_id, "ok": False, "outcome": "aborted"}
        try:
            for step in steps:
                record = pinned[step.tool]
                if result.stop_reason is not None:
                    result.steps.append(StepResult(step.id, record.label, "skipped"))
                    continue
                if self._clock() - began >= budget_s:
                    result.stop_reason = "time_budget"
                    result.steps.append(
                        StepResult(
                            step.id,
                            record.label,
                            "failed",
                            error=CallError(
                                type="ChainTimeBudget",
                                message=f"the chain has run for {budget_s} seconds, so this step was not started",
                                hint="run the remaining steps in a new chain, or call them with forge_run",
                            ),
                        )
                    )
                    continue
                step_result = self._step(step, record, outputs, chain_id)
                result.steps.append(step_result)
                if step_result.status == "ok":
                    outputs[step.id] = step_result.output
                else:
                    result.stop_reason = "step_failed"
            result.duration_ms = max(0, round((self._clock() - began) * 1000))
            finished = self._summary(result)
            return result
        except BaseException as exc:
            finished["error_type"] = type(exc).__name__  # our own classes: no tool text, no host detail
            raise
        finally:
            try:
                self._ledger_factory(path, chain_id).append("forge", "forge.chain.finished", finished)
            except Exception:  # the outcome is known; say so loudly rather than lose the result
                log.exception("chain %s finished but its ledger event could not be written", chain_id)

    def _step(self, step: Step, record: VersionRecord, outputs: dict[str, Any], chain_id: str) -> StepResult:
        try:
            args = substitute(step.args, outputs)
            if not isinstance(args, dict):
                raise _StepProblem(
                    f"args: the reference gave {_kind(args)}, but the arguments of a tool must be an object"
                )
        except _StepProblem as exc:
            return StepResult(
                step.id,
                record.label,
                "failed",
                error=CallError(
                    type="BadReference",
                    message=f"step `{step.id}`, {exc}",
                    hint="read the earlier step's output and reference a value that is there",
                ),
            )
        try:
            call = self._runner.run(record, args, allow_status=RUNNABLE_STATUSES, chain_id=chain_id)
        except (
            ToolNotRunnable,
            RegistryError,
        ) as exc:  # the version changed under the chain: that step failed, nothing else did
            return StepResult(
                step.id,
                record.label,
                "failed",
                error=CallError(
                    type="NotRunnable",
                    message=textsafety.sanitize(exc.message, max_chars=300),
                    hint=textsafety.sanitize(exc.hint or "", max_chars=300),
                ),
            )
        ran = call.error is None or call.error.type not in NOT_RUN_ERRORS
        return StepResult(
            step.id,
            record.label,
            "ok" if call.ok else "failed",
            run_id=call.call_id if ran else None,
            duration_ms=int(call.duration_ms) if ran else None,
            output=call.result if call.ok else None,
            error=None if call.ok else call.error,
            stdout_tail=call.stdout_tail,
            stderr_tail=call.stderr_tail,
            truncated=bool(call.truncated),
            timed_out=bool(call.sandbox_timed_out),
        )

    @staticmethod
    def _summary(result: ChainResult) -> dict[str, Any]:
        """The `forge.chain.finished` payload: ids, statuses, hashes and sizes. Never an output, an argument or an error text."""
        rows: list[dict[str, Any]] = []
        for step in result.steps:
            row: dict[str, Any] = {
                "id": step.id,
                "tool": step.tool,
                "status": step.status,
                "run_id": step.run_id,
            }
            if step.status == "ok":
                row["output_sha256"], row["output_bytes"] = _digest(step.output)
            if step.error is not None:
                row["error_type"] = step.error.type
            rows.append(row)
        return {
            "chain_id": result.chain_id,
            "ok": result.ok,
            "outcome": "finished",
            "stop_reason": result.stop_reason,
            "duration_ms": result.duration_ms,
            "steps": rows,
        }


__all__ = [
    "HARD_MAX_STEPS",
    "MAX_COPY_BYTES",
    "RUNNABLE_STATUSES",
    "STEP_STATUSES",
    "STOP_REASONS",
    "ChainResult",
    "ChainRunner",
    "Ref",
    "RefError",
    "Step",
    "StepResult",
    "parse_ref",
    "parse_request",
    "substitute",
]
