"""`SandboxRunner` on top of MiniLocker: hardened Docker containers, an allowlisting egress proxy, decoys and a hash-chained ledger.

Uses MiniLocker's `Sandbox`, `EgressManager`, `Watch`, `Ledger` and `snapshot` and nothing else of it. It does not wrap
`run_task` or MiniLocker's API, and it does not hold a sandbox open across calls (sessions are P13). MiniLocker is only reached
through `_ml_path.load`, so `import toolforge` never needs Docker.

Security properties, and the tests that prove them:
* every sandbox is non-root, read-only, capability-less and without a route out unless the spec names hosts, because it is
  MiniLocker's `Sandbox` (`tests/docker/test_minilocker_runner.py` reads Docker's own account back through `hardening`);
* `close` always destroys the container, also when the audit or the ledger fails (`tests/test_minilocker_runner.py`);
* the egress proxy is one process-wide singleton, shared only when its allowlist already covers the request and never restarted
  under a running sandbox (`tests/test_egress_pool.py`);
* decoy values and environment contents are never logged, only the ids and labels `Watch` produces.

Known limits, written down on purpose. The proxy has one global allowlist, so a tool can reach any host approved for any tool
that ran since the proxy started (the allowlist only grows until `shutdown`). The same is true of brokered services: the proxy
cannot tell sandboxes apart, so a sandbox attached to it can ask for any service the proxy was started with, and MiniLocker hands
every sandbox the placeholder variables of all of them. MiniLocker's proxy also lets a host's subdomains through (an allowed
`example.org` allows `www.example.org`). Two ToolForge processes on one Docker daemon share the fixed proxy container name and
would remove each other's proxy. Egress events are read when `run` returns, not while it runs. `docs/approvals.md` and
`docs/upstream/minilocker-per-sandbox-allowlist.md` describe the consequences and the fix.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

from toolforge import textsafety
from toolforge.config import RUNTIME_IMAGE, Settings
from toolforge.errors import SandboxError, SandboxUnavailable
from toolforge.ledger import ToolforgeLedger
from toolforge.sandbox import _ml_path
from toolforge.sandbox.base import (
    AuditSummary,
    ContextHandle,
    RunOutput,
    SandboxSpec,
    check_run_args,
    normalize_path,
    split_events,
)

# `$1` is the destination; nothing from the caller is ever spliced into the script text.
_PUT_SCRIPT = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
_PUT_TIMEOUT_S = 60


def _docker_errors() -> tuple[type[BaseException], ...]:
    """What a failing Docker daemon, pull or container start raises: docker's own errors, requests' transport errors, OSError."""
    try:
        import docker  # the Docker SDK, not MiniLocker; imported here so `import toolforge` never needs it
        import requests
    except (
        ImportError
    ):  # no SDK: nothing can raise its errors, and this runs inside an `except`, so it must not raise
        return (OSError,)
    return (docker.errors.DockerException, requests.exceptions.RequestException, OSError)


def configured_broker_services(
    settings: Settings | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """The credential-broker services the operator configured, as MiniLocker reads them (`broker_from_env`).

    `{name: {"host", "header", "prefix", "token", "placeholder"}}`. The tokens are in the result because the proxy needs them, so
    callers that only want to know what exists must use `broker_service_hosts`. `MINILOCKER_BROKER` and
    `MINILOCKER_BROKER_TOKEN_<NAME>` are MiniLocker's own variables, read from the process environment (not from `.env`, which only
    holds `TOOLFORGE_` settings). A service with no token, or with a malformed name or host, does not exist.
    """
    module = _ml_path.load("minilocker.egress.manager", settings)
    return dict(module.broker_from_env(environ))


def broker_service_hosts(
    settings: Settings | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """`{service name: host}` of the configured broker services. Never a token. Raises `SandboxUnavailable` when MiniLocker or the
    Docker SDK cannot be imported (a broker cannot work then either)."""
    return {name: str(entry["host"]) for name, entry in configured_broker_services(settings, environ).items()}


def _start_proxy(
    hosts: list[str], broker: Mapping[str, dict[str, Any]] | None = None, *, image: str = ""
) -> Any:
    """Start MiniLocker's egress proxy for `hosts` and `broker`. The default factory of `EgressPool`.

    With `image` (`TOOLFORGE_EGRESS_PROXY_IMAGE`) the proxy runs MiniLocker's packaged module from that image instead of bind-mounting a
    file from this process's filesystem, which is what a ToolForge that runs inside a container needs.
    """
    module = _ml_path.load("minilocker.egress.manager")
    try:
        extra: dict[str, Any] = {"image": image, "packaged": True} if image else {}
        manager = module.EgressManager(hosts, broker=dict(broker or {}), **extra)
        return manager.start()
    except SandboxUnavailable:
        raise
    except Exception as exc:  # Docker errors and "egress proxy failed to start" (a RuntimeError)
        raise SandboxUnavailable(
            f"the egress proxy could not start: {type(exc).__name__}: {str(exc)[:200]}",
            hint="run `toolforge doctor`",
        ) from exc


class EgressPool:
    """Owns the one `EgressManager` this process may have (MiniLocker's proxy and its networks have fixed names).

    `acquire(hosts, services)` returns the running manager when `hosts` and `services` are already covered, restarts the proxy
    with the union of the old allowlist and `hosts` (and the old and new brokered services) only when no sandbox is using it, and
    otherwise raises `SandboxUnavailable("egress is busy")`. Every successful `acquire` must be paired with one `release`.

    The factory is called as `factory(hosts)`, or `factory(hosts, broker=...)` once a brokered service is involved; `broker_loader`
    turns service names into the entries the proxy needs (it raises `SandboxUnavailable` for a name that is not configured, and
    only the services asked for ever reach the proxy, never every service the operator configured).
    """

    def __init__(
        self,
        factory: Callable[..., Any] | None = None,
        *,
        broker_loader: Callable[[Iterable[str]], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._factory = factory or _start_proxy
        self._broker_loader = broker_loader or _load_broker
        self._lock = threading.Lock()
        self._manager: Any = None
        self._allowed: set[str] = set()
        self._services: set[str] = set()
        self._users = 0

    @property
    def allowed(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._allowed)

    @property
    def services(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._services)

    @property
    def users(self) -> int:
        with self._lock:
            return self._users

    def acquire(self, hosts: Iterable[str], services: Iterable[str] = ()) -> Any:
        wanted = {str(h).strip().lower() for h in hosts}
        wanted_services = {str(n).strip().lower() for n in services}
        with self._lock:
            if self._manager is not None and wanted <= self._allowed and wanted_services <= self._services:
                self._users += 1
                return self._manager
            if self._users > 0:
                raise SandboxUnavailable(
                    "egress is busy",
                    hint="another sandbox is using the egress proxy with a different host or service list; retry when it closes",
                )
            union = sorted(self._allowed | wanted)
            union_services = sorted(self._services | wanted_services)
            try:
                if union_services:
                    manager = self._factory(union, broker=self._broker_loader(union_services))
                else:
                    manager = self._factory(union)
            except BaseException:
                # Starting a proxy removes the previous one first, so whatever ran before is gone: start from nothing.
                self._manager, self._allowed, self._services = None, set(), set()
                raise
            self._manager, self._allowed, self._services, self._users = (
                manager,
                set(union),
                set(union_services),
                1,
            )
            return manager

    def release(self) -> None:
        with self._lock:
            if self._users > 0:
                self._users -= 1

    def shutdown(self) -> bool:
        """Stop the proxy if no sandbox uses it. True when it is stopped (or never ran), False when it is still in use."""
        with self._lock:
            if self._users > 0:
                return False
            manager, self._manager, self._allowed, self._services = self._manager, None, set(), set()
        if manager is not None:
            manager.stop()
        return True


def _load_broker(names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The configured entries for exactly `names`. A name the operator did not configure is an error, never skipped."""
    configured = configured_broker_services()
    wanted = sorted(set(names))
    missing = [name for name in wanted if name not in configured]
    if missing:
        raise SandboxUnavailable(
            f"no credential broker service is configured for: {', '.join(missing)}",
            hint=(
                "the operator sets MINILOCKER_BROKER=<name>=<host> and MINILOCKER_BROKER_TOKEN_<NAME> in the environment of the "
                "process that runs ToolForge (see docs/approvals.md)"
            ),
        )
    return {name: configured[name] for name in wanted}


class MiniLockerHandle(ContextHandle):
    """One MiniLocker sandbox. Used by one thread at a time."""

    def __init__(
        self,
        *,
        box: Any,
        watch: Any,
        ledger: ToolforgeLedger,
        spec: SandboxSpec,
        manager: Any,
        pool: EgressPool | None,
        snapshot: Any,
        started_at: float,
    ) -> None:
        self._box = box
        self._watch = watch
        self._ledger = ledger
        self._spec = spec
        self._manager = manager
        self._pool = pool
        self._snapshot = snapshot
        self._t0 = started_at
        self._first_event = len(
            ledger.events
        )  # a resumed ledger already holds earlier runs; this handle's start
        self._egress_seen = 0
        self._egress_unobserved_logged = False
        self._opened_hardening: dict = {}
        self._released = manager is None
        self._summary: AuditSummary | None = None
        self.closed = False

    # -- helpers --------------------------------------------------------------------------------------------------------
    def _log(self, actor: str, type: str, payload: dict | None = None) -> dict:
        return self._ledger.append(actor, type, payload)

    @property
    def name(self) -> str:
        return str(self._box.name)

    def _check_usable(self) -> None:
        if self.closed:
            raise SandboxError("the sandbox is closed", hint="open a new one")
        if getattr(self._box, "dead", False):
            raise SandboxError(
                "the sandbox was killed (a command ran past its timeout)", hint="open a new one"
            )

    def _flush_egress(self) -> None:
        """Forward the proxy's events for this container to the ledger and to `Watch`, as MiniLocker's agent loop does.

        Reading the proxy is observability and must not break the command it observes, but a failure to read is itself recorded:
        "no egress events" must never silently mean "nothing was attempted".
        """
        if self._manager is None:
            return
        try:
            events = self._manager.events_for(self._box.ip, self._t0)
        except Exception as exc:
            if not self._egress_unobserved_logged:
                self._egress_unobserved_logged = True
                self._log("forge", "forge.egress.unobserved", {"reason": type(exc).__name__})
            return
        for event in events[self._egress_seen :]:
            payload = {
                ("proxy_ts" if key == "ts" else key): value
                for key, value in event.items()
                if key != "decision"
            }
            payload["source"] = "code"
            self._log("egress", "egress." + str(event.get("decision", "unknown")), payload)
            self._watch.on_egress(event, self._log)
        self._egress_seen = len(events)

    # -- the SandboxHandle protocol ---------------------------------------------------------------------------------------
    def put(self, path: str, data: bytes) -> None:
        self._check_usable()
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("data must be bytes")
        target = normalize_path(path)
        result = self._box.exec_stdin(
            ["sh", "-c", _PUT_SCRIPT, "put", target], bytes(data), timeout_s=_PUT_TIMEOUT_S, max_out=4096
        )
        if result.timed_out or result.exit_code != 0:
            why = (
                "timed out"
                if result.timed_out
                else f"exit {result.exit_code}: {textsafety.sanitize(result.stderr, max_chars=200)!r}"
            )
            raise SandboxError(
                f"could not write {target} into the sandbox ({why})",
                hint="the workspace is a size-capped tmpfs; check the file size against workspace_mb",
            )

    def run(self, argv: list[str], timeout_s: int, max_out: int) -> RunOutput:
        args = check_run_args(argv, timeout_s, max_out)
        self._check_usable()
        raw = self._box.exec_capped(args, timeout_s=timeout_s, max_bytes=max_out)
        output = RunOutput(
            int(raw.exit_code), bytes(raw.stdout), bytes(raw.stderr), bool(raw.timed_out), bool(raw.truncated)
        )
        self._flush_egress()
        if self._spec.decoys:
            # Complete, not abbreviated: a decoy past the first few hundred characters must still be noticed. The text is only
            # searched; Watch logs ids and labels, never the text.
            text = output.stdout.decode("utf-8", "replace") + "\n" + output.stderr.decode("utf-8", "replace")
            self._watch.after_tool(self._box, "run", text, self._log, probe=True)
        return output

    def freeze(self) -> bytes:
        self._check_usable()
        try:
            return bytes(self._snapshot.capture(self._box))
        except self._snapshot.SnapshotError as exc:
            raise SandboxError(f"could not freeze the workspace: {exc}") from exc

    def thaw(self, blob: bytes) -> None:
        self._check_usable()
        if not isinstance(blob, (bytes, bytearray)) or not blob:
            raise SandboxError("thaw needs the bytes that freeze returned")
        try:
            self._snapshot.restore(self._box, bytes(blob))  # raises unless tar exited 0 within its timeout
        except self._snapshot.SnapshotError as exc:
            raise SandboxError(f"could not restore the workspace: {exc}") from exc
        if getattr(self._box, "dead", False):
            raise SandboxError("the sandbox was killed while the workspace was restored")

    def events(self) -> list[dict]:
        return copy.deepcopy(self._ledger.events)

    def close(self) -> AuditSummary:
        """Audit, then destroy, then report. The container is destroyed even when the audit or the ledger raises."""
        if self._summary is not None:
            return self._summary
        error: BaseException | None = None
        hardening: dict = {}
        measured: dict = {}
        try:
            self._flush_egress()
            hardening = (
                self._box.hardening() or self._opened_hardening
            )  # must be read before the container is removed
            before = len(self._ledger.events)
            self._watch.audit(self._box, "code", self._log)
            audits = [e for e in self._ledger.events[before:] if e.get("type") == "sandbox.audit"]
            measured = dict(audits[-1].get("payload") or {}) if audits else {}
        except BaseException as exc:
            error = exc
        destroyed, destroy_error = self._destroy()
        self._release()
        if destroyed:
            try:
                self._log("sandbox", "sandbox.destroyed", self._destroyed_payload())
            except BaseException as exc:
                error = error or exc
        else:
            error = error or destroy_error
        decoys, egress = split_events(self._ledger.events[self._first_event :])
        self._summary = AuditSummary(
            hardening=dict(hardening),
            measured=measured,
            decoy_events=decoys,
            egress_attempts=egress,
            ledger_path=self._spec.ledger_path,
            destroyed=destroyed,
        )
        self.closed = True
        if error is not None:
            raise error
        return self._summary

    def _destroyed_payload(self) -> dict:
        payload: dict = {"name": self.name, "reason": "close"}
        if self._spec.label:
            payload["label"] = textsafety.sanitize(self._spec.label, max_chars=200)
        return payload

    def _destroy(self) -> tuple[bool, SandboxError | None]:
        try:
            self._box.destroy()
            return True, None
        except Exception as exc:
            if type(exc).__name__ == "NotFound":  # docker.errors.NotFound: the container is already gone
                return True, None
            return False, SandboxError(
                f"could not remove the sandbox container {self.name}: {type(exc).__name__}",
                hint="run `toolforge doctor --clean` or `docker rm -f` the container",
            )

    def _release(self) -> None:
        if not self._released and self._pool is not None:
            self._released = True
            self._pool.release()


class MiniLockerRunner:
    """Opens MiniLocker sandboxes. Safe to use from several threads: it holds no per-sandbox state of its own."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        pool: EgressPool | None = None,
        sandbox_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        image = settings.egress_proxy_image if settings is not None else ""
        self.pool = pool or EgressPool(partial(_start_proxy, image=image) if image else None)
        self._sandbox_factory = sandbox_factory  # tests pass a stand-in; production uses minilocker's Sandbox

    def shutdown(self) -> bool:
        """Stop the egress proxy when no sandbox uses it. Call once when the process is done with sandboxes."""
        return self.pool.shutdown()

    def open(self, spec: SandboxSpec) -> MiniLockerHandle:
        if not isinstance(spec, SandboxSpec):
            raise TypeError("open() takes a SandboxSpec")
        _ml_path.ensure_importable(self._settings)
        factory = (
            self._sandbox_factory
            or _ml_path.load("minilocker.sandbox.docker_provider", self._settings).Sandbox
        )
        watch_module = _ml_path.load("minilocker.agent.watch", self._settings)
        snapshot = _ml_path.load("minilocker.sandbox.snapshot", self._settings)
        task_id = Path(spec.ledger_path).stem if spec.ledger_path else f"sandbox-{uuid.uuid4().hex[:12]}"
        ledger = ToolforgeLedger(
            spec.ledger_path, task_id
        )  # before any container exists, so a ledger problem leaks nothing

        manager = None
        if spec.network != "none" or spec.broker:
            # The broker is a path on the proxy, so a sandbox that only needs a brokered service is attached to it with no host.
            manager = self.pool.acquire(() if spec.network == "none" else spec.network, spec.broker)
        box = None
        handle: MiniLockerHandle | None = None
        reason = "open_failed"
        try:
            started_at = time.time()
            box = self._create_box(factory, spec, manager)
            handle = MiniLockerHandle(
                box=box,
                watch=watch_module.Watch(strict=False),
                ledger=ledger,
                spec=spec,
                manager=manager,
                pool=self.pool if manager is not None else None,
                snapshot=snapshot,
                started_at=started_at,
            )
            self._record_created(handle, spec)
            if spec.decoys:
                reason = "decoys_unavailable"
                self._plant_decoys(handle)
            return handle
        except BaseException:
            # The one place a half-opened sandbox is torn down: destroy it once, give the proxy back, and say so in the ledger.
            if box is not None:
                destroyed = False
                try:
                    box.destroy()
                    destroyed = True
                except Exception as exc:
                    destroyed = type(exc).__name__ == "NotFound"
                if destroyed and handle is not None:
                    try:
                        handle._log("sandbox", "sandbox.destroyed", {"name": handle.name, "reason": reason})
                    except Exception:
                        pass  # the original error is the one to report
            if manager is not None:
                self.pool.release()
            raise

    @staticmethod
    def _create_box(factory: Callable[..., Any], spec: SandboxSpec, manager: Any) -> Any:
        try:
            return factory(
                image=spec.image,
                workspace_mb=spec.workspace_mb,
                egress=manager,
                mem_limit=f"{spec.memory_mb}m",
                pids_limit=spec.pids,
                extra_env=dict(spec.env),
            )
        except SandboxUnavailable:
            raise
        except _docker_errors() as exc:
            raise SandboxUnavailable(
                f"Docker could not start the sandbox: {type(exc).__name__}: {str(exc)[:200]}",
                hint=(
                    "build the runtime image once with `toolforge images build` (it is local, so it cannot be pulled), then run `toolforge doctor`"
                    if spec.image == RUNTIME_IMAGE
                    else "run `toolforge doctor`; the first run needs internet to pull the image"
                ),
            ) from exc

    @staticmethod
    def _record_created(handle: MiniLockerHandle, spec: SandboxSpec) -> None:
        """Log `sandbox.created` with the same payload shape as MiniLocker's loop, so its report builder understands the file."""
        box = handle._box
        created: dict = {"name": box.name, "ip": box.ip}
        hardening = box.hardening()
        if hardening:
            created["hardening"] = hardening
            handle._opened_hardening = hardening
        if spec.label:
            created["label"] = textsafety.sanitize(spec.label, max_chars=200)
        handle._log("sandbox", "sandbox.created", created)

    @staticmethod
    def _plant_decoys(handle: MiniLockerHandle) -> None:
        """Plant MiniLocker's decoys. If they cannot be planted the sandbox is refused: a containment run without decoys would
        pass vacuously."""
        handle._watch.attach(handle._box, handle._log)
        if handle._watch.canaries is None:
            reasons = [
                e["payload"].get("reason") for e in handle._ledger.events if e["type"] == "canary.unavailable"
            ]
            raise SandboxUnavailable(  # `open` destroys the box and records why
                f"decoy credentials were requested but could not be planted ({reasons[-1] if reasons else 'unknown reason'})",
                hint="run `toolforge doctor`",
            )
