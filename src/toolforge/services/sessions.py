"""Sessions (P13): a sandbox that stays open across calls, with snapshots, rewind, limits and an audit when it ends.

A tool whose manifest says `kind: session` is called as `run(args, state)`. The harness loads `state` from `/workspace/.tf/state.json`
before the call and saves it after a call that succeeded (JSON only, at most 256 KB). `forge_run` on such a tool without a `session_id`
opens a session and returns the id; later calls pass it.

How it works, and the tests that prove it (`tests/test_sessions.py` unless named):

* **One sandbox, one process per call.** The session's sandbox is opened once (network, limits, decoys exactly as a single call would
  get them) and every call runs the harness as a new process in it. Before every call the host puts the harness, the SDK and the
  frozen package back, because the tool runs as the same user and may have overwritten its own files
  (`test_the_package_and_harness_are_restored_before_every_call`).
* **Steps and snapshots.** After a call that succeeded the host freezes the workspace and keeps the last `KEEP_SNAPSHOTS` (5) in memory,
  each at most `MAX_SNAPSHOT_BYTES`. Step 0 is the state before the first call. A call that failed is not a step (the state was not saved
  either). `rewind(step)` opens a new sandbox, restores the snapshot, checks that the restored state has the hash it was recorded with,
  and only then replaces the old one (`test_rewind_restores_the_right_snapshot`).
* **Dead sandboxes are replaced.** A call that times out or kills its sandbox leaves the session in a fresh sandbox restored to the last
  step, and the caller is told so (`test_a_timeout_restores_the_last_step`). If that fails the session ends as `lost`.
* **Limits.** Idle for `session_ttl_s`, alive for `session_max_s`, at most `max_sessions` open (counted in the registry, so a second
  process counts too). The next open waits `OPEN_WAIT_S` and then fails with a clear message. A reaper thread (started with the first
  session) closes what expired, what the operator asked to close, and what belongs to a quarantined version
  (`test_idle_and_max_lifetime_expiry`, `test_max_sessions`).
* **Whose session.** MCP carries no caller identity, so a session id is a capability: `s-<8 hex>.<32 hex>`. Only the SHA-256 of the
  secret half is stored, a wrong or unknown id gets one and the same answer, and the public half (`s-<8 hex>`) is what lists and the
  operator's commands show (`test_a_caller_cannot_use_another_callers_session`).
* **Ledgers.** The sandbox's own ledger file is written by the sandbox's runner while it is open, so the host writes to it only before it
  opens and after it is closed (`s-<public>-<n>.jsonl` in the version's folder, one per sandbox, with `forge.call.started` and
  `forge.call.finished` like a single call; the Blast Radius report samples them as real calls). What the *session* did is a second
  file, `sessions/<public>.jsonl`, with `forge.session.opened`, `.step`, `.rewound`, `.recovered` and `.closed`, written only by the host.
  History before a rewind stays in it. Arguments and results never enter either, only their hashes and sizes.
* **Tripwires.** The decoy check of P08 runs after every call, against the events the open sandbox has logged. A decoy touched
  quarantines the version, discards the answer and ends every session of that version. The real-secret audit needs the sandbox to be
  destroyed, so it runs when a sandbox is closed (the session's end, or a rewind or recovery replacing it): a real secret found
  there quarantines the version too, but answers given earlier in the session were already delivered (documented in DEVIATIONS.md).
* **Quarantine closes sessions; deprecation does not.** A session is bound to the tool version it opened with.

Nothing here approves, promotes or releases a tool, and a caller can only touch the session whose id it holds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from toolforge.errors import Busy, SandboxError, ToolNotRunnable
from toolforge.models.results import CallError, CallResult
from toolforge.registry import lifecycle
from toolforge.registry import sessions as store
from toolforge.sandbox import harness
from toolforge.sandbox.base import SandboxHandle, SandboxSpec, decoy_violations
from toolforge.services.runner import (
    ENVELOPE_OVERHEAD,
    ESCAPE_FACTOR,
    GRACE_S,
    WORKSPACE_MB,
    _Outcome,
    _plain,
    _requested,
    _shown,
    _Target,
    _unusable,
    parse_envelope,
    repack_blob,
    sdk_source,
)

if TYPE_CHECKING:
    from toolforge.services.runner import ToolRunner

log = logging.getLogger(__name__)

KEEP_SNAPSHOTS = 5
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
OPEN_WAIT_S = 5.0
OPEN_POLL_S = 0.25
REAP_INTERVAL_S = 5.0
BUSY_WAIT_S = 30.0  # a second call on a session waits this long for the first to end
STATE_CHECK_TIMEOUT_S = 30
EMPTY_STATE_SHA = hashlib.sha256(
    b"{}"
).hexdigest()  # the state before the first call, as the harness reports it
SESSIONS_DIR = "sessions"
REASONS = {
    "closed": "it was closed",
    "idle": "it was idle for longer than the idle limit",
    "max_lifetime": "it reached its maximum lifetime",
    "quarantined": "its tool version was quarantined",
    "operator": "the operator closed it",
    "lost": "its sandbox was lost and could not be restored",
    "open_failed": "its first call failed",
    "shutdown": "the server was stopped",
}


@dataclass
class _Snapshot:
    step: int
    blob: bytes | None  # None: it was larger than MAX_SNAPSHOT_BYTES or could not be taken
    state_sha256: str | None
    state_bytes: int | None


@dataclass
class _Live:
    """A session whose sandbox this process holds."""

    public: str
    target: _Target
    packed: bytes
    limits: dict[str, int]
    network: tuple[str, ...]
    brokered: tuple[str, ...]
    handle: SandboxHandle
    box_no: int
    box_ledger: str
    events_ledger: str
    spec: SandboxSpec
    lock: threading.RLock = field(default_factory=threading.RLock)
    step: int = 0
    snapshots: OrderedDict[int, _Snapshot] = field(default_factory=OrderedDict)
    violations_seen: int = 0
    closed: bool = False
    close_info: dict[str, Any] = field(default_factory=dict)
    quarantined: bool = False


def _ended(why: str) -> ToolNotRunnable:
    return ToolNotRunnable(
        f"that session has ended: {why}",
        code="session_ended",
        hint="start a new one by calling forge_run without session_id; state from the old session is gone",
    )


def _bad_session() -> ToolNotRunnable:
    return ToolNotRunnable(
        "that session id is not valid",
        code="bad_session",
        hint="use the session_id that forge_run returned for this tool; ids cannot be guessed or shared",
    )


def _canonical_state_sha(info: Mapping[str, Any]) -> str | None:
    sha = info.get("state_sha256")
    return sha if isinstance(sha, str) and len(sha) == 64 else None


class SessionManager:
    """Opens, runs, rewinds and closes sessions for one `ToolRunner`. Safe to use from several threads (one call per session at a time)."""

    def __init__(
        self,
        tools: ToolRunner,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        reaper: bool = True,
    ) -> None:
        self._tools = tools
        self._clock = clock
        self._sleep = sleep
        self._reaper_wanted = reaper
        self._owner = secrets.token_hex(8)
        self._live: dict[str, _Live] = {}
        self._guard = threading.Lock()  # protects `_live`, held only for a dict access
        self._open_lock = (
            threading.Lock()
        )  # one open at a time in this process: the slot check and the reservation are one step
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- small helpers ------------------------------------------------------------------------------------------------------

    @property
    def _registry(self) -> Any:
        registry = self._tools._registry
        if registry is None:
            raise ToolNotRunnable(
                "this runner has no registry, and sessions are kept there",
                code="no_registry",
                hint="sessions need a registered tool version",
            )
        return registry

    @property
    def _settings(self) -> Any:
        return self._tools._settings

    def _ttl(self) -> int:
        return int(self._settings.session_ttl_s)

    def _max(self) -> int:
        return int(self._settings.session_max_s)

    def _events_path(self, target: _Target, public: str) -> str:
        return str(
            Path(self._settings.home) / "ledgers" / target.ledger_dir / SESSIONS_DIR / f"{public}.jsonl"
        )

    def _box_path(self, target: _Target, public: str, box_no: int) -> str:
        return str(Path(self._settings.home) / "ledgers" / target.ledger_dir / f"{public}-{box_no}.jsonl")

    def _event(self, live: _Live, event: str, payload: dict[str, Any]) -> None:
        """One event in the session's own ledger. A ledger that cannot be written stops the call that needed it (no step without a record)."""
        self._tools._ledger_factory(live.events_ledger, live.public).append(
            "forge", event, {"session": live.public, "tool": live.target.label, **payload}
        )

    def live_count(self) -> int:
        with self._guard:
            return len(self._live)

    # -- resolving an id ----------------------------------------------------------------------------------------------------

    def resolve(self, session_id: object) -> store.SessionRow:
        """The open session this id names, or `ToolNotRunnable`. An unknown id and a wrong secret get the same answer."""
        parts = store.split_session_id(session_id)
        row = self._registry.get_session(parts[0]) if parts is not None else None
        if parts is None or row is None or not store.secret_matches(row.secret_sha256, parts[1]):
            raise _bad_session()
        if row.is_open:
            due = row.due(self._clock())
            if due is not None and row.public_id in self._live:
                self._close(self._live[row.public_id], "expired", due)
                row = self._registry.get_session(row.public_id) or row
        if not row.is_open:
            raise _ended(REASONS.get(row.reason or "", row.reason or row.state))
        return row

    def _live_for(self, session_id: object, version_id: int | None) -> _Live:
        row = self.resolve(session_id)
        if version_id is not None and row.version_id != version_id:
            raise ToolNotRunnable(
                "that session belongs to a different tool version",
                code="session_other_tool",
                hint="a session is bound to the tool version that opened it; use the same tool, or start a new session",
            )
        with self._guard:
            live = self._live.get(row.public_id)
        if live is None:
            raise ToolNotRunnable(
                "that session is held by another ToolForge process, so this one cannot run it",
                code="session_elsewhere",
                hint="call the server that opened it, or start a new session",
            )
        return live

    # -- the call -----------------------------------------------------------------------------------------------------------

    def call(
        self,
        target: _Target,
        args: dict,
        files: Mapping[str, bytes] | None,
        session_id: str | None,
        record_stats: bool = True,
    ) -> CallResult:
        """One call of a session tool. Without `session_id` it opens a session; a failed first call closes it again."""
        tools = self._tools
        call_id = tools._new_id()
        refusal = tools._check_args(target.manifest, args) or tools._check_files(target.manifest, files)
        if refusal is not None:  # the caller's mistake: nothing opens, nothing is recorded
            return CallResult(ok=False, error=refusal, version=target.label, call_id=call_id)
        attached = {path: bytes(data) for path, data in (files or {}).items()}
        full_id: str | None = None
        if session_id is None:
            live, full_id = self._open(target)
        else:
            live = self._live_for(session_id, target.version_id)
            full_id = session_id
        opened = session_id is None
        try:
            result = self._step(live, call_id, args, attached, record_stats)
        except BaseException:
            if opened:
                self._close(live, "closed", "open_failed")
            raise
        if opened and not result.ok:
            self._close(live, "closed", "open_failed")
            return result
        if live.closed:  # the call ended the session (a decoy, a lost sandbox)
            return result
        result.session_id = full_id
        return result

    # -- opening -------------------------------------------------------------------------------------------------------------

    def _slots_free(self) -> bool:
        now = self._clock()
        open_rows = [r for r in self._registry.list_sessions(state="open", limit=1000) if not r.orphaned(now)]
        return len(open_rows) < int(self._settings.max_sessions)

    def _open(self, target: _Target) -> tuple[_Live, str]:
        tools = self._tools
        if target.version_id is None or target.unverified:
            raise ToolNotRunnable(
                f"{target.label} is not a registered version, and sessions are only kept for registered ones",
                code="session_needs_registry",
                hint="submit the tool and have the operator promote it",
            )
        manifest = target.manifest
        wants = _requested(manifest)
        hosts, brokered = tuple(wants["network"]), tuple(wants["secrets"])
        if hosts or brokered:
            tools._authorize(target, hosts, brokered)
        limits = tools._limits(manifest)
        packed, package_files = repack_blob(target.load_blob())
        entry = str(manifest["entrypoint"]).rpartition(":")[0]
        if entry not in package_files:
            raise _unusable(f"the entrypoint file {_shown(entry)} is not in it")
        deadline = self._clock() + OPEN_WAIT_S
        while True:
            self._sweep_locked_out()
            with self._open_lock:
                if self._slots_free():
                    session_id, public, secret_sha = store.new_session_id()
                    self._reserve(target, public, secret_sha)
                    break
            if self._clock() >= deadline:
                raise Busy(
                    f"{self._settings.max_sessions} sessions are already open",
                    hint="wait for one to end (an idle session expires after "
                    f"{self._ttl() // 60} minutes) or close yours with forge_session_close, then try again",
                )
            self._sleep(OPEN_POLL_S)
        # The row exists (state open) with no box yet; a failure from here on closes it.
        try:
            live = self._open_box(target, public, packed, limits, hosts, brokered, box_no=1)
        except BaseException:
            self._registry.close_session(public, "lost", "the sandbox could not be opened", now=self._clock())
            raise
        live.snapshots[0] = _Snapshot(0, self._freeze(live), EMPTY_STATE_SHA, 2)
        with self._guard:
            self._live[public] = live
        self._registry.touch_session(
            public, now=self._clock(), ttl_s=self._ttl(), box=self._box_name(live), steps=0
        )
        self._event(
            live,
            "forge.session.opened",
            {
                "code_sha": target.code_sha,
                "limits": limits,
                "network": list(hosts) or "none",
                "secrets": list(brokered),
                "ttl_s": self._ttl(),
                "max_s": self._max(),
                "image": self._settings.runtime_image,
                "image_id": self._tools._image_id(self._settings.runtime_image),
            },
        )
        self._start_reaper()
        return live, session_id

    def _reserve(self, target: _Target, public: str, secret_sha: str) -> None:
        now = self._clock()
        self._registry.open_session(
            public_id=public,
            secret_sha256=secret_sha,
            version_id=int(target.version_id or 0),
            label=target.label,
            owner=self._owner,
            box=None,
            now=now,
            ttl_s=self._ttl(),
            max_s=self._max(),
        )

    def _spec(
        self,
        target: _Target,
        limits: dict[str, int],
        hosts: tuple[str, ...],
        brokered: tuple[str, ...],
        ledger: str,
    ) -> SandboxSpec:
        return SandboxSpec(
            image=self._settings.runtime_image,
            network=hosts or "none",
            broker=brokered,
            workspace_mb=WORKSPACE_MB,
            memory_mb=limits["memory_mb"],
            decoys=self._settings.runtime_decoys,
            ledger_path=ledger,
            label=f"session:{target.label}",
        )

    def _open_box(
        self,
        target: _Target,
        public: str,
        packed: bytes,
        limits: dict[str, int],
        hosts: tuple[str, ...],
        brokered: tuple[str, ...],
        *,
        box_no: int,
    ) -> _Live:
        """A new sandbox for the session, with its ledger started first and the package, harness and SDK in place."""
        tools = self._tools
        ledger = self._box_path(target, public, box_no)
        spec = self._spec(target, limits, hosts, brokered, ledger)
        # Before any sandbox exists: if this cannot be written, nothing opens (the same rule as a single call).
        tools._append(
            ledger,
            f"{public}-{box_no}",
            "forge.call.started",
            {
                "call_id": f"{public}-{box_no}",
                "tool": target.label,
                "code_sha": target.code_sha,
                "unverified": False,
                "session_id": public,
                "box": box_no,
                "limits": limits,
                "network": list(hosts) or "none",
                "secrets": list(brokered),
            },
        )
        try:
            handle = tools._runner.open(spec)
        except BaseException:
            self._finish_box_ledger(ledger, f"{public}-{box_no}", {"ok": False, "error_type": "NotOpened"})
            raise
        try:
            handle.thaw(packed)
            self._put_runtime(handle)
        except BaseException:
            summary = tools._close(handle)
            self._finish_box_ledger(
                ledger,
                f"{public}-{box_no}",
                {"ok": False, "error_type": "NotReady", **self._plain_summary(summary)},
            )
            raise
        return _Live(
            public=public,
            target=target,
            packed=packed,
            limits=limits,
            network=hosts,
            brokered=brokered,
            handle=handle,
            box_no=box_no,
            box_ledger=ledger,
            events_ledger=self._events_path(target, public),
            spec=spec,
        )

    @staticmethod
    def _put_runtime(handle: SandboxHandle) -> None:
        handle.put(harness.HARNESS_PATH, harness.HARNESS_SOURCE.encode("utf-8"))
        handle.put(harness.SDK_PATH, sdk_source())

    @staticmethod
    def _plain_summary(summary: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in summary.items() if k not in ("violations", "real_secrets")}

    def _finish_box_ledger(self, ledger: str, call_id: str, payload: dict[str, Any]) -> None:
        try:
            self._tools._append(ledger, call_id, "forge.call.finished", {"call_id": call_id, **payload})
        except Exception:
            log.exception("the ledger %s could not be finished", ledger)

    @staticmethod
    def _box_name(live: _Live) -> str | None:
        name = getattr(live.handle, "name", None)
        return _plain(name, 80) if isinstance(name, str) else None

    def _freeze(self, live: _Live) -> bytes | None:
        try:
            blob = live.handle.freeze()
        except SandboxError:
            log.warning("a snapshot of session %s could not be taken", live.public, exc_info=True)
            return None
        return blob if len(blob) <= MAX_SNAPSHOT_BYTES else None

    # -- one step -------------------------------------------------------------------------------------------------------------

    def _lock(self, live: _Live) -> None:
        if not live.lock.acquire(timeout=BUSY_WAIT_S):
            raise Busy(
                "another call on this session is still running",
                hint="wait for it to finish; a session runs one call at a time",
            )

    def _step(
        self, live: _Live, call_id: str, args: dict, attached: dict[str, bytes], record_stats: bool
    ) -> CallResult:
        tools = self._tools
        target = live.target
        self._lock(live)
        try:
            if live.closed:
                raise _ended(REASONS["lost"])
            now = self._clock()
            row = self._registry.get_session(live.public)
            if row is None or not row.is_open:
                self._close(live, "lost", "lost")
                raise _ended(REASONS["lost"])
            if row.due(now) is not None:
                self._close(live, "expired", row.due(now) or "idle")
                raise _ended(REASONS.get(row.due(now) or "idle", "it expired"))
            record = self._registry.get_version(target.version_id)
            if record is not None and record.status == lifecycle.QUARANTINED:
                self._close(live, "quarantined", "quarantined")
                raise _ended(REASONS["quarantined"])
            nonce = secrets.token_hex(16)
            request = json.dumps(
                {
                    "args": args,
                    "entrypoint": target.manifest["entrypoint"],
                    "limits": live.limits,
                    "session": True,
                    "input_files": sorted(attached),
                }
            ).encode("utf-8")
            args_json = json.dumps(args, sort_keys=True).encode("utf-8")
            args_sha = hashlib.sha256(args_json).hexdigest()
            output = None
            started = tools._clock()
            try:
                live.handle.thaw(live.packed)  # the tool's own files, as they were frozen
                self._put_runtime(live.handle)
                live.handle.put(harness.REQUEST_PATH, request)
                for path, data in attached.items():
                    live.handle.put(f"{harness.WORKSPACE}/{path}", data)
                output = live.handle.run(
                    ["python", harness.HARNESS_PATH, nonce],
                    live.limits["timeout_s"] + GRACE_S,
                    live.limits["output_kb"] * 1024 * ESCAPE_FACTOR + ENVELOPE_OVERHEAD,
                )
            except SandboxError:  # the container went away under the call
                output = None
            duration_ms = max(0, round((tools._clock() - started) * 1000))
            outcome = tools._interpret(output, nonce, live.limits)
            return self._finish_step(
                live, call_id, outcome, duration_ms, args, attached, args_sha, len(args_json), record_stats
            )
        finally:
            live.lock.release()

    def _finish_step(
        self,
        live: _Live,
        call_id: str,
        outcome: _Outcome,
        duration_ms: int,
        args: dict,
        attached: Mapping[str, bytes],
        args_sha: str,
        args_bytes: int,
        record_stats: bool,
    ) -> CallResult:
        tools = self._tools
        target = live.target
        info: dict[str, Any] = {}
        violations = decoy_violations(self._events(live)) if live.spec.decoys else []
        fresh = violations[live.violations_seen :]
        live.violations_seen = len(violations)
        note = ""
        if fresh:  # the tripwire: nothing the tool produced is kept, and the version is pulled with every session it has
            outcome = tools._decoy_outcome(fresh)
            info["decoy_violations"] = len(fresh)
            info["quarantined"] = tools._quarantine(target, call_id, fresh)
            live.quarantined = True
        elif outcome.error is not None and (
            outcome.error.type in ("Timeout", "SandboxCrashed") or self._box_dead(live)
        ):
            note = self._recover(live)
        elif outcome.ok:
            live.step += 1
            blob = self._freeze(live)
            for later in [s for s in live.snapshots if s >= live.step]:
                del live.snapshots[later]  # a timeline that a rewind left behind
            live.snapshots[live.step] = _Snapshot(live.step, blob, outcome.state_sha256, outcome.state_bytes)
            while len(live.snapshots) > KEEP_SNAPSHOTS:
                live.snapshots.popitem(last=False)
            info["snapshot_bytes"] = len(blob) if blob is not None else None
        if note and outcome.error is not None:
            outcome.error = CallError(
                type=outcome.error.type,
                message=outcome.error.message,
                hint=f"{outcome.error.hint}; {note}".strip("; "),
                trace_tail=outcome.error.trace_tail,
            )
        step_event = {
            "step": live.step,
            "call_id": call_id,
            "ok": outcome.ok,
            "error_type": outcome.error.type if outcome.error else None,
            "args_sha256": args_sha,
            "args_bytes": args_bytes,
            "files": len(attached),
            "duration_ms": duration_ms,
            "result_bytes": outcome.result_bytes,
            "state_sha256": outcome.state_sha256,
            "state_bytes": outcome.state_bytes,
            "truncated": outcome.truncated,
            "sandbox_timed_out": outcome.sandbox_timed_out,
            **info,
        }
        try:
            self._event(live, "forge.session.step", step_event)
        finally:
            if record_stats:
                tools._record(target, outcome, duration_ms, live.public, call_id)
                health = tools._after_failure(target, outcome, args, attached, call_id)
                if health:
                    step_event.update(health)
            if not live.closed:
                self._registry.touch_session(
                    live.public,
                    now=self._clock(),
                    ttl_s=self._ttl(),
                    box=self._box_name(live),
                    steps=live.step,
                )
        if live.quarantined and not live.closed:
            self._close(live, "quarantined", "quarantined")
        remaining = max(0, int(self._expires_in(live)))
        return CallResult(
            ok=outcome.ok,
            result=outcome.result,
            error=outcome.error,
            stdout_tail=outcome.stdout_tail,
            stderr_tail=outcome.stderr_tail,
            duration_ms=duration_ms,
            version=target.label,
            call_id=call_id,
            truncated=outcome.truncated,
            sandbox_timed_out=outcome.sandbox_timed_out,
            step=live.step,
            session_expires_in_s=None if live.closed else remaining,
        )

    def _expires_in(self, live: _Live) -> float:
        row = self._registry.get_session(live.public)
        return (row.expires_at - self._clock()) if row is not None else 0.0

    def _events(self, live: _Live) -> list[dict]:
        try:
            return live.handle.events()
        except Exception:
            log.warning("the events of session %s could not be read", live.public, exc_info=True)
            return []

    @staticmethod
    def _box_dead(live: _Live) -> bool:
        return bool(getattr(live.handle, "dead", False) or getattr(live.handle, "closed", False))

    # -- replacing a sandbox --------------------------------------------------------------------------------------------------

    def _swap_box(self, live: _Live, snapshot: _Snapshot) -> None:
        """Open a new sandbox, restore `snapshot` into it, check the state, and only then close the old one. Raises on any failure
        and leaves the old sandbox (if it still works) in place."""
        if snapshot.blob is None:
            raise SandboxError("that step has no snapshot")
        box_no = live.box_no + 1
        fresh = self._open_box(
            live.target, live.public, live.packed, live.limits, live.network, live.brokered, box_no=box_no
        )
        try:
            fresh.handle.thaw(snapshot.blob)
            self._put_runtime(fresh.handle)
            self._check_state(fresh.handle, snapshot)
        except BaseException:
            summary = self._tools._close(fresh.handle)
            self._finish_box_ledger(
                fresh.box_ledger,
                f"{live.public}-{box_no}",
                {"ok": False, "error_type": "NotRestored", **self._plain_summary(summary)},
            )
            raise
        old_handle, old_ledger, old_no, old_seen = (
            live.handle,
            live.box_ledger,
            live.box_no,
            live.violations_seen,
        )
        live.handle, live.box_ledger, live.box_no, live.spec = (
            fresh.handle,
            fresh.box_ledger,
            box_no,
            fresh.spec,
        )
        live.violations_seen = 0
        self._close_box(live, old_handle, old_ledger, old_no, "replaced", old_seen)

    def _check_state(self, handle: SandboxHandle, snapshot: _Snapshot) -> None:
        """Ask the restored sandbox for the hash of the state it holds and compare it with the one recorded at the snapshot."""
        if snapshot.state_sha256 is None:
            return
        nonce = secrets.token_hex(16)
        handle.put(harness.REQUEST_PATH, b"{}")
        output = handle.run(
            ["python", harness.HARNESS_PATH, nonce, "--state"], STATE_CHECK_TIMEOUT_S, 64 * 1024
        )
        envelope = parse_envelope(output.stdout, nonce) if not output.timed_out else None
        reported = (
            envelope.result
            if envelope is not None and envelope.ok and isinstance(envelope.result, dict)
            else {}
        )
        if _canonical_state_sha(reported) != snapshot.state_sha256:
            raise SandboxError("the restored state does not match the state that was saved at that step")

    def _close_box(
        self, live: _Live, handle: SandboxHandle, ledger: str, box_no: int, why: str, seen: int
    ) -> dict[str, Any]:
        """Destroy one sandbox, audit it, finish its ledger, and act on what the audit found. Returns what `close` reports."""
        tools = self._tools
        summary = tools._close(handle)
        found = summary.pop("violations", [])
        real = summary.pop("real_secrets", [])
        if not live.spec.decoys:
            found = []
        late = found[seen:]
        quarantined = False
        if late:
            quarantined = tools._quarantine(live.target, f"{live.public}-{box_no}", late)
        elif real:
            quarantined = tools._quarantine(
                live.target,
                f"{live.public}-{box_no}",
                (),
                why=f"a real secret was found inside the sandbox of session {live.public} ({', '.join(real)})",
            )
        payload = {
            "ok": not (late or real),
            "error_type": None if not (late or real) else "PolicyViolation",
            "why": why,
            **summary,
        }
        if late:
            payload["decoy_violations"] = len(late)
        if real:
            payload["real_secrets_found"] = len(real)
        if quarantined:
            payload["quarantined"] = True
            live.quarantined = True
        self._finish_box_ledger(ledger, f"{live.public}-{box_no}", payload)
        return {
            "real_secret_audit": summary.get("real_secret_audit", "not_measured"),
            "cleanup_ok": summary.get("cleanup_ok", False),
            "quarantined": quarantined or live.quarantined,
        }

    def _recover(self, live: _Live) -> str:
        """The sandbox died under a call: put the session in a new one at the last step. Returns a sentence for the caller."""
        last = max(live.snapshots) if live.snapshots else None
        snapshot = live.snapshots.get(last) if last is not None else None
        try:
            if snapshot is None or snapshot.blob is None:
                raise SandboxError("there is no snapshot to restore")
            self._swap_box(live, snapshot)
        except Exception:
            log.warning("session %s could not be restored after its sandbox died", live.public, exc_info=True)
            self._close(live, "lost", "lost")
            return "the session ended because its sandbox could not be restored; start a new one"
        live.step = snapshot.step
        for later in [s for s in live.snapshots if s > snapshot.step]:
            del live.snapshots[later]
        try:
            self._event(live, "forge.session.recovered", {"to_step": snapshot.step, "box": live.box_no})
        except Exception:
            log.exception("the recovery of session %s could not be recorded", live.public)
        return f"the session was restored to step {snapshot.step} in a new sandbox"

    # -- rewind --------------------------------------------------------------------------------------------------------------

    def rewind(self, session_id: object, step: object) -> dict[str, Any]:
        """Restore the snapshot of `step` into a new sandbox. The history before the rewind stays in the session's ledger."""
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise ToolNotRunnable(
                "step must be a whole number, 0 or more",
                code="bad_step",
                hint="forge_session_list with your session_id shows which steps can be restored",
            )
        live = self._live_for(session_id, None)
        self._lock(live)
        try:
            if live.closed:
                raise _ended(REASONS["lost"])
            snapshot = live.snapshots.get(step)
            if snapshot is None or snapshot.blob is None:
                available = sorted(s for s, snap in live.snapshots.items() if snap.blob is not None)
                raise ToolNotRunnable(
                    f"step {step} cannot be restored",
                    code="step_not_available",
                    hint=f"the steps that can be restored are {available} (the last {KEEP_SNAPSHOTS} are kept)",
                )
            came_from = live.step
            try:
                self._swap_box(live, snapshot)
            except SandboxError as exc:
                raise ToolNotRunnable(
                    f"step {step} could not be restored ({_plain(exc.message, 120)})",
                    code="rewind_failed",
                    hint="the session is unchanged; try another step or close it",
                ) from None
            live.step = step
            for later in [s for s in live.snapshots if s > step]:
                del live.snapshots[later]
            self._registry.note_session_rewind(live.public, box=self._box_name(live), steps=step)
            self._registry.touch_session(
                live.public, now=self._clock(), ttl_s=self._ttl(), box=self._box_name(live), steps=step
            )
            self._event(
                live, "forge.session.rewound", {"from_step": came_from, "to_step": step, "box": live.box_no}
            )
            result = {
                "session": live.public,
                "step": step,
                "from_step": came_from,
                "steps_available": sorted(s for s, snap in live.snapshots.items() if snap.blob is not None),
                "expires_in_s": max(0, int(self._expires_in(live))),
            }
            if live.quarantined:
                self._close(live, "quarantined", "quarantined")
                raise _ended(REASONS["quarantined"])
            return result
        finally:
            live.lock.release()

    # -- closing -------------------------------------------------------------------------------------------------------------

    def close(self, session_id: object) -> dict[str, Any]:
        """End the caller's own session: audit it and produce its Blast Radius report. Closing twice is answered, not an error."""
        parts = store.split_session_id(session_id)
        row = self._registry.get_session(parts[0]) if parts is not None else None
        if parts is None or row is None or not store.secret_matches(row.secret_sha256, parts[1]):
            raise _bad_session()
        if not row.is_open:
            return {
                "session": row.public_id,
                "closed": True,
                "already_closed": True,
                "state": row.state,
                "reason": row.reason,
                "steps": row.steps,
            }
        live = self._live_for(session_id, None)
        info = self._close(live, "closed", "closed")
        return info

    def _close(self, live: _Live, state: str, reason: str) -> dict[str, Any]:
        """Close a session this process holds. Idempotent; the second caller gets what the first one reported."""
        self._lock(live)
        try:
            if live.closed:
                return live.close_info
            live.closed = True
            audit = self._close_box(
                live, live.handle, live.box_ledger, live.box_no, "session_end", live.violations_seen
            )
            if live.quarantined and state not in ("quarantined",):
                state, reason = "quarantined", "quarantined"
            now = self._clock()
            self._registry.close_session(live.public, state, reason, now=now)
            report = self._blast_radius(live)
            info: dict[str, Any] = {
                "session": live.public,
                "closed": True,
                "state": state,
                "reason": reason,
                "steps": live.step,
                "real_secret_audit": audit["real_secret_audit"],
                "cleanup_ok": audit["cleanup_ok"],
                "quarantined": audit["quarantined"],
                "blast_radius": report,
            }
            live.close_info = info
            try:
                self._event(
                    live,
                    "forge.session.closed",
                    {k: v for k, v in info.items() if k not in ("blast_radius", "session")},
                )
            except Exception:
                log.exception("the end of session %s could not be recorded", live.public)
            with self._guard:
                self._live.pop(live.public, None)
            live.snapshots.clear()  # the memory goes with the session
            return info
        finally:
            live.lock.release()

    def _blast_radius(self, live: _Live) -> dict[str, Any] | None:
        """The session's own Blast Radius report, stored next to its ledgers. None when it could not be made (never an error)."""
        try:
            from toolforge.report import blast_radius

            return blast_radius.session_report(
                self._settings, live.target.manifest["name"], live.target.manifest["version"], live.public
            )
        except Exception:
            log.warning("the Blast Radius report of session %s could not be made", live.public, exc_info=True)
            return None

    def close_for_version(
        self, version_id: int, state: str = "quarantined", reason: str = "quarantined"
    ) -> int:
        """End every session this process holds for a version (quarantine). Returns how many were closed."""
        with self._guard:
            mine = [live for live in self._live.values() if live.target.version_id == version_id]
        for live in mine:
            self._close(live, state, reason)
        try:  # sessions held by another process: its reaper sees the status and closes them within a few seconds
            for row in self._registry.open_sessions_of(version_id):
                if row.public_id not in {live.public for live in mine}:
                    self._registry.request_session_close(row.public_id)
        except Exception:
            log.warning(
                "could not ask other processes to close the sessions of a quarantined version", exc_info=True
            )
        return len(mine)

    # -- listing -------------------------------------------------------------------------------------------------------------

    def describe(self, session_id: object | None = None) -> dict[str, Any]:
        """With a session id: that session in detail (its snapshots too). Without: every open session by its public name only."""
        now = self._clock()
        if session_id is not None:
            row = self.resolve(session_id)
            with self._guard:
                live = self._live.get(row.public_id)
            entry = self._row_view(row, now, held=live is not None)
            if live is not None:
                entry["steps_available"] = sorted(
                    s for s, snap in live.snapshots.items() if snap.blob is not None
                )
            return {"session": entry}
        rows = [r for r in self._registry.list_sessions(state="open", limit=50) if not r.orphaned(now)]
        return {
            "sessions": [self._row_view(r, now, held=r.public_id in self._live) for r in rows],
            "max_sessions": int(self._settings.max_sessions),
            "note": "ids shown here are public names; they cannot be used to run, rewind or close a session",
        }

    @staticmethod
    def _row_view(row: store.SessionRow, now: float, *, held: bool) -> dict[str, Any]:
        return {
            "session": row.public_id,
            "tool": row.label,
            "state": row.state,
            "steps": row.steps,
            "rewinds": row.rewinds,
            "age_s": max(0, int(now - row.opened_at)),
            "idle_s": max(0, int(now - row.last_used_at)),
            "expires_in_s": max(0, int(row.expires_at - now)),
            "max_remaining_s": max(0, int(row.hard_expires_at - now)),
            "held_here": held,
        }

    # -- the reaper ----------------------------------------------------------------------------------------------------------

    def _sweep_locked_out(self) -> None:
        """Close what is due, but never wait for a session that is busy (its own call will notice)."""
        now = self._clock()
        with self._guard:
            held = list(self._live.values())
        for live in held:
            if not live.lock.acquire(blocking=False):
                continue
            try:
                if live.closed:
                    continue
                self._sweep_one(live, now)
            except Exception:
                log.warning("the reaper could not check session %s", live.public, exc_info=True)
            finally:
                live.lock.release()

    def _sweep_one(self, live: _Live, now: float) -> None:
        row = self._registry.get_session(live.public)
        if row is None or not row.is_open:
            self._close(live, "lost", "lost")
            return
        due = row.due(now)
        if due is not None:
            self._close(live, "expired", due)
            return
        if row.close_requested:
            self._close(live, "operator", "operator")
            return
        record = self._registry.get_version(live.target.version_id)
        if record is not None and record.status == lifecycle.QUARANTINED:
            self._close(live, "quarantined", "quarantined")

    def sweep(self) -> None:
        """One pass of the reaper (the thread calls it every `REAP_INTERVAL_S`; tests call it with their own clock)."""
        try:
            self._registry.session_heartbeat(self._owner, now=self._clock())
        except Exception:
            log.warning("the session heartbeat could not be written", exc_info=True)
        self._sweep_locked_out()

    def _start_reaper(self) -> None:
        if not self._reaper_wanted or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        thread = threading.Thread(target=self._reap_forever, name="toolforge-session-reaper", daemon=True)
        self._thread = thread
        thread.start()

    def _reap_forever(self) -> None:
        while not self._stop.wait(REAP_INTERVAL_S):
            try:
                self.sweep()
            except Exception:
                log.warning("the session reaper failed once", exc_info=True)

    def shutdown(self) -> int:
        """Close every session this process holds (server stop). Returns how many."""
        self._stop.set()
        with self._guard:
            mine = list(self._live.values())
        count = 0
        for live in mine:
            try:
                self._close(live, "closed", "shutdown")
                count += 1
            except Exception:
                log.warning("session %s could not be closed at shutdown", live.public, exc_info=True)
        return count


__all__ = [
    "KEEP_SNAPSHOTS",
    "MAX_SNAPSHOT_BYTES",
    "SessionManager",
]
