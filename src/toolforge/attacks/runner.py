"""Runs the attack catalogue against the real stack and turns what was observed into verdicts.

Each attack gets a `Lab`: a fresh temporary `TOOLFORGE_HOME` (registry, blobs, ledgers), the real `Forge`, `ToolRunner` and MCP server
wired to hardened Docker sandboxes, and a **host canary** (a file and an environment variable on this machine that hold a random
token no sandbox should ever see). Attacks run one after another under a hard deadline, and every sandbox container an attack leaves
behind is removed afterwards and reported.

Beyond the attack's own checks every attack gets three run-wide checks, so a breach that the attack did not aim at is still seen:

* the host canary is intact and its token appears in no file of the data directory (no host secret reached a ledger or the registry);
* every ledger the attack produced verifies as a hash chain (skipped for the attack that edits one on purpose);
* every sandbox that a ledger says was created was destroyed, and Docker lists no container the attack left behind.

An attack that cannot run for lack of Docker, the runtime image or MiniLocker is `skipped`, never `contained`. An attack that runs past
its deadline, or whose driver raises, is `inconclusive`. Evidence is only what the ledger, the registry and Docker's own report say;
see `catalog.py` for what each attack observes. Proven by `tests/test_attacks_runner.py` (verdict plumbing, deadline, skipping, the
run-wide checks, the report) and `tests/test_attacks_drivers.py` (the attacks that need no sandbox, run for real).

This module never runs tool code. A hostile package only ever runs inside a sandbox opened by `MiniLockerRunner`.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import logging
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from toolforge import __version__, cli_run, ledger
from toolforge.attacks import catalog
from toolforge.attacks.catalog import (
    BREACHED,
    CONTAINED,
    FAIL,
    INCONCLUSIVE,
    PASS,
    SKIP,
    SKIPPED,
    Attack,
    CheckResult,
)
from toolforge.config import Settings
from toolforge.errors import SandboxError, SandboxUnavailable, ToolForgeError
from toolforge.models.package import ToolPackage
from toolforge.models.results import CallResult
from toolforge.provenance import attest, bundle, keys
from toolforge.registry import Registry
from toolforge.report import blast_radius
from toolforge.sandbox import _ml_path
from toolforge.sandbox.base import real_rootfs_changes
from toolforge.services import approvals, operator_actions
from toolforge.services.forge import Forge
from toolforge.services.runner import ToolRunner
from toolforge.verify.g1_static import StaticGate, analyse
from toolforge.verify.g5_challenger import ChallengerGate
from toolforge.verify.pipeline import GateContext, GateResult, VerificationReport, default_gates, passed

DEADLINE_S = 300
FUZZ_INPUTS = 10  # G4 inputs in the lab: enough to exercise the gate, few enough that twenty attacks finish
SUBMITTER = "attacker"
RUNS_DIR = Path("runs") / "attacks"
FOOTER = (
    "A passing attack suite is a statement about this catalogue, not about all possible attacks. 'contained' means the observations "
    "listed for the attack showed the defence holding in this run; it is evidence, not proof."
)
_BIG_FILE = 8 * 1024 * 1024


class DeadlineExceeded(Exception):
    pass


@dataclass
class AttackResult:
    id: str
    title: str
    verdict: str
    evidence: str
    checks: list[CheckResult] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "seconds": round(self.seconds, 1),
            "checks": [c.as_dict() for c in self.checks],
        }


# --- Seeding victims -------------------------------------------------------------------------------------------------------


class SeedGate:
    """A gate that passes without looking, used only to put a *victim* tool in the registry (`Lab.seed`), never to test a defence.

    The summary says so, and the verdict of no attack rests on a result of one of these. Where an attack needs to see a real gate
    (G1 for a hostile description, say) the real one is used and only the gates after it are seeded.
    """

    budget_s = 60

    def __init__(self, gate_id: str) -> None:
        self.id = gate_id

    def run(self, ctx: GateContext) -> GateResult:
        return passed(f"seeded by the attack lab: {self.id} did not run for this victim tool")


class SeedBuild(SeedGate):
    """The seeded G2 stores the package as the version's blob, which promotion needs."""

    def __init__(self) -> None:
        super().__init__("G2")

    def run(self, ctx: GateContext) -> GateResult:
        if ctx.registry is not None and ctx.version is not None:
            ctx.registry.attach_blob(ctx.version.id, ctx.registry.blobs.put(ctx.package.to_tar()))
        return super().run(ctx)


# --- What a submission looked like -----------------------------------------------------------------------------------------


@dataclass
class Submission:
    name: str
    version: str
    report: VerificationReport | None = None
    error: ToolForgeError | None = None

    @property
    def registered(self) -> bool:
        return bool(self.report is not None and self.report.registered)

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}" if self.registered else ""

    @property
    def status(self) -> str | None:
        return self.report.status if self.report is not None else None

    def obs(self) -> dict[str, Any]:
        gates: dict[str, Any] = {}
        if self.report is not None:
            gates = {
                g.id: {
                    "status": g.status,
                    "summary": g.summary,
                    "hints": list(g.hints),
                    "metrics": dict(g.metrics),
                }
                for g in self.report.gates
            }
        return {
            "registered": self.registered,
            "status": self.status,
            "error_code": getattr(self.error, "code", None),
            "gates": gates,
        }


# --- The Lab ---------------------------------------------------------------------------------------------------------------


class _NoSandbox:
    def open(self, spec: Any) -> Any:
        raise SandboxUnavailable("this attack runs without a sandbox", hint="it should not open one")


class _CountingRunner:
    """Stands in for the tool runner of the MCP attacks: it records every call and runs nothing."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, record: Any, args: Any, **kwargs: Any) -> CallResult:
        self.calls += 1
        return CallResult(ok=True, result={"ran": True}, call_id="counted")


class Listener:
    """A real streamable HTTP listener on loopback, behind `NetGuard`, in a thread. Nothing leaves the machine."""

    def __init__(self, app: Any, port: int) -> None:
        import uvicorn

        from toolforge.server.app import build_http_app

        self.port = port
        config = uvicorn.Config(
            build_http_app(app), host="127.0.0.1", port=port, log_level="error", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> Listener:
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the loopback listener did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(10)

    def post(
        self, body: dict[str, Any], *, token: str | None, origin: str | None = None, host: str | None = None
    ) -> int | None:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("POST", "/mcp", json.dumps(body), headers)
                response = conn.getresponse()
                status = response.status
                with contextlib.suppress(Exception):
                    response.read(65536)
                return status
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            return None

    def initialize(self, *, token: str) -> int | None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "attack-lab", "version": "1"},
            },
        }
        return self.post(body, token=token)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def docker_problem(settings: Settings) -> str | None:
    """Why a sandbox attack cannot run here, or None when a daemon answers and the runtime image is built."""
    try:
        import docker

        client = docker.from_env(timeout=5)
    except Exception as exc:
        return f"needs Docker: it could not be reached ({type(exc).__name__})"
    try:
        client.ping()
        client.images.get(settings.runtime_image)
    except Exception as exc:
        if type(exc).__name__ == "ImageNotFound":
            return f"needs the runtime image {settings.runtime_image}: run `toolforge images build`"
        return f"needs Docker: no daemon answered ({type(exc).__name__})"
    finally:
        client.close()
    return None


class Stranger:
    """Somebody else's ToolForge: its own data directory, signing key and registry, outside the lab's home.

    It verifies tools with scripted gates and exports them as signed bundles, which is all an attacker needs to hand the lab (the
    receiver) a bundle that is genuine in every way except the ones an attack changes. It holds the key it signs with, so it can
    also re-sign a bundle it edited, which is exactly what a hostile author can do.
    """

    def __init__(self, root: Path, name: str, gates: Sequence[Any]) -> None:
        self.home = root / name
        self.home.mkdir(parents=True)
        self.settings = Settings(
            home=self.home,
            llm_base_url=None,
            llm_model=None,
            llm_api_key=None,
            challenger_base_url=None,
            challenger_model=None,
            challenger_api_key=None,
            server_token=None,
        )
        self.fingerprint = keys.init_keys(self.home).fingerprint
        self.public_path = self.home / "keys" / "signing.pub"
        self._private, self.raw_public = keys.load_private_key(self.home)
        self.registry = Registry(self.home)
        self._forge = Forge(
            self.settings, self.registry, None, ledger_factory=cli_run.make_ledger, gates=list(gates)
        )

    def export(self, package: ToolPackage, out: Path) -> dict[str, bytes]:
        """Verify `package` here (scripted gates) and export it. Returns the bundle's members, ready to be edited."""
        self._forge.submit_package(package, "stranger")
        name, version = _ident(package)
        bundle.export_bundle(
            self.settings,
            self.registry,
            f"{name}@{version}",
            out,
            now=lambda: "2026-11-26T12:00:00.000Z",
            image_lookup=lambda _image: None,
        )
        return bundle.read_members(out.read_bytes())

    def resign(self, members: dict[str, bytes], **changes: Any) -> dict[str, bytes]:
        """The members with the attestation's fields changed and signed again with this key (a valid signature over the edit)."""
        body = json.loads(members["attestation.json"])
        for name in ("signer", "signer_key", "signature"):
            body.pop(name, None)
        body.update(changes)
        signed = attest.sign(body, self._private, self.raw_public)
        return {**members, "attestation.json": attest.canonical_json(signed)}

    def claim_to_be(self, members: dict[str, bytes], fingerprint: str) -> dict[str, bytes]:
        """The attestation with `signer` set to somebody else's fingerprint, signed with this key and carrying this key."""
        body = {
            k: v
            for k, v in json.loads(members["attestation.json"]).items()
            if k not in ("signer", "signer_key", "signature")
        }
        forged = {**body, "signer": fingerprint, "signer_key": base64.b64encode(self.raw_public).decode()}
        forged["signature"] = base64.b64encode(self._private.sign(attest.canonical_json(forged))).decode()
        return {**members, "attestation.json": attest.canonical_json(forged)}

    def close(self) -> None:
        self.registry.close()


class Lab:
    """One attack's world. Everything an attack does goes through the real stack, into `home`, which is deleted afterwards."""

    def __init__(
        self,
        home: Path,
        *,
        runner: Any = None,
        docker_ok: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.home = Path(home)
        self.settings = settings or Settings(
            home=self.home,
            llm_base_url=None,
            llm_model=None,
            llm_api_key=None,
            challenger_base_url=None,
            challenger_model=None,
            challenger_api_key=None,
            server_token=None,
        )
        self.registry = Registry(self.home)
        self._runner = runner
        self._problem: str | None = None if docker_ok is not False else "needs Docker (not available)"
        if docker_ok is None:
            self._problem = docker_problem(self.settings)
        self._tool_runner: ToolRunner | None = None
        self._apps: list[Any] = []
        self._counting = _CountingRunner()
        self.leftovers: list[str] = []
        self.scratch = Path(
            tempfile.mkdtemp(prefix="toolforge-attack-scratch-")
        )  # other people's homes and bundle files
        self._strangers: list[Stranger] = []
        self._containers_before = self._sandbox_containers()
        # The host canary: a file and a variable on this machine that no sandbox can see (it has no mounts and no inherited
        # environment). Seeing the token anywhere under `home` means a host secret reached a ledger or the registry.
        self._canary_dir = Path(tempfile.mkdtemp(prefix="toolforge-attack-canary-"))
        self.canary_token = "HOSTCANARY-" + secrets.token_hex(12)
        self.canary_path = self._canary_dir / "host_canary.txt"
        self.canary_path.write_text(self.canary_token, encoding="utf-8")
        self.canary_env = "ATTACKLAB_CANARY_" + secrets.token_hex(4).upper()
        os.environ[self.canary_env] = self.canary_token

    # -- availability ---------------------------------------------------------------------------------------------------

    @property
    def runner(self) -> Any:
        if self._runner is None:
            self._runner = (
                cli_run.make_sandbox_runner(self.settings) if self._problem is None else _NoSandbox()
            )
        return self._runner

    def unavailable_reason(self, attack: Attack) -> str | None:
        if not _ml_path.minilocker_available():
            return "MiniLocker is not available: run `git submodule update --init third_party/minilocker`"
        if attack.needs_docker and self._problem is not None:
            return self._problem
        return None

    # -- the stack ------------------------------------------------------------------------------------------------------

    def _forge(self, gates: Sequence[Any] | None = None, challenger: Any = None) -> Forge:
        if gates is None:
            gates = default_gates(fuzz_inputs=FUZZ_INPUTS)
            if challenger is not None:
                gates = [ChallengerGate(client=challenger) if g.id == "G5" else g for g in gates]
        return Forge(
            self.settings, self.registry, self.runner, ledger_factory=cli_run.make_ledger, gates=list(gates)
        )

    def seeded_gates(self) -> list[Any]:
        return [SeedGate("G1"), SeedBuild(), SeedGate("G3"), SeedGate("G4")]

    def static_then_seeded(self) -> list[Any]:
        return [StaticGate(), SeedBuild(), SeedGate("G3"), SeedGate("G4")]

    def submit(
        self, package: ToolPackage, *, gates: Sequence[Any] | None = None, challenger: Any = None
    ) -> Submission:
        name, version = _ident(package)
        try:
            report = self._forge(gates, challenger).submit_package(package, SUBMITTER)
        except (SandboxUnavailable, SandboxError):
            raise
        except ToolForgeError as exc:  # a refusal before any gate ran (a name collision, a changed version)
            return Submission(name, version, None, exc)
        return Submission(name, version, report, None)

    def seed(self, package: ToolPackage, *, promote: bool = True) -> Submission:
        """Put a victim tool in the registry through scripted gates, and (by default) make it active as the operator would."""
        sub = self.submit(package, gates=self.seeded_gates())
        if not sub.registered:
            raise RuntimeError(f"the victim {sub.name} could not be seeded: {sub.status}")
        if promote:
            self.promote(sub.label)
        return sub

    def promote(self, label: str) -> None:
        operator_actions.promote(
            self.settings,
            self.registry,
            label,
            "attack lab: the operator's step",
            ledger_factory=cli_run.make_ledger,
        )

    def tool_runner(self) -> ToolRunner:
        if self._tool_runner is None:
            self._tool_runner = ToolRunner(
                self.runner, self.registry, self.settings, cli_run.make_ledger, session_reaper=False
            )
        return self._tool_runner

    def run(self, label: str, args: dict[str, Any]) -> CallResult:
        return self.tool_runner().run(label, args)

    def run_unverified(self, package: ToolPackage, args: dict[str, Any]) -> CallResult:
        return self.tool_runner().run_unverified(package, args)

    def refused_code(self, label: str, args: dict[str, Any]) -> str | None:
        """The error code when a call is refused before it runs, None when it ran."""
        try:
            self.run(label, args)
        except ToolForgeError as exc:
            return exc.code
        return None

    def approve(self, label: str) -> None:
        """The operator approves a version that asked for permissions (a step of the lab's own, never an attack's evidence)."""
        record = self._record(label)
        request = approvals.latest_approval(self.registry, record.id) if record else None
        if request is None:
            raise RuntimeError(f"{label} has no approval request to answer")
        operator_actions.decide(
            self.settings,
            self.registry,
            request.id,
            "approve",
            "attack lab: the operator's step",
            ledger_factory=cli_run.make_ledger,
        )

    def quarantine(self, label: str) -> None:
        operator_actions.quarantine(
            self.settings,
            self.registry,
            label,
            "attack lab: the operator's step",
            ledger_factory=cli_run.make_ledger,
        )

    def chain_service(self, *, enabled: bool) -> Any:
        """A `ToolService` with `chain_enabled` as given over the counting runner, so a step that ran is a call that is counted."""
        settings = self.settings.model_copy(update={"chain_enabled": enabled})
        return self._counting_app(settings).service

    def stranger(self, name: str) -> Stranger:
        who = Stranger(self.scratch, name, self.seeded_gates())
        self._strangers.append(who)
        return who

    def trust(self, who: Stranger, label: str) -> str:
        """The operator trusts somebody's key (`toolforge trust`). Returns the fingerprint."""
        return keys.trust_key(self.home, who.public_path, label).fingerprint

    def write_bundle(self, name: str, data: bytes) -> Path:
        path = self.scratch / name
        path.write_bytes(data)
        return path

    def import_bundle(self, path: Path) -> tuple[bundle.ImportResult, Submission]:
        """`toolforge import`: verify the bundle, then run the receiver's own pipeline (real G1, scripted G2 to G4) over what it holds."""
        forge = self._forge(self.static_then_seeded())
        done = bundle.import_bundle(path, self.settings, self.registry, forge.submit_package)
        attested = done.verified.attestation
        return done, Submission(attested.name, attested.version, done.report, None)

    def created_by_of(self, label: str) -> str | None:
        record = self._record(label)
        return record.created_by if record else None

    def home_snapshot(self) -> dict[str, str]:
        """Every file of the data directory and a hash of it, so \"nothing changed\" is measured and not assumed."""
        found: dict[str, str] = {}
        for path in sorted(self.home.rglob("*")):
            if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                try:
                    found[str(path.relative_to(self.home))] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
        return found

    def service(self) -> Any:
        """The MCP server's own `ToolService` over the real runner (used for `forge_find` and `forge_run` as an agent would)."""
        from toolforge.server.app import build_server

        if not self._apps:
            self._apps.append(
                build_server(
                    self.settings,
                    registry=self.registry,
                    sandbox=self.runner,
                    ledger_factory=cli_run.make_ledger,
                    forge=self._forge(),
                    tool_runner=self.tool_runner(),
                    runtime_probe=lambda image: None,
                )
            )
        return self._apps[0].service

    # -- the MCP server with a runner that counts ------------------------------------------------------------------------

    def _counting_app(self, settings: Settings | None = None) -> Any:
        from toolforge.server.app import build_server

        app = build_server(
            settings or self.settings,
            registry=self.registry,
            sandbox=object(),
            ledger_factory=cli_run.make_ledger,
            forge=self._forge(self.seeded_gates()),
            tool_runner=self._counting,  # type: ignore[arg-type]
            runtime_probe=lambda image: None,
        )
        self._apps.append(app)
        return app

    def _mcp(self, work: Callable[[Any], Any], settings: Settings | None = None) -> Any:
        import anyio
        from mcp import Client

        app = self._counting_app(settings)

        async def main() -> Any:
            async with Client(app.server) as client:
                return await work(client)

        return anyio.run(main)

    def mcp_tool_names(self) -> list[str]:
        return self.mcp_tool_names_for(None)

    def mcp_tool_names_for(self, chain_enabled: bool | None) -> list[str]:
        """The tools an MCP client is offered, with the server's `chain_enabled` setting as given (the lab's own when `None`)."""

        async def work(client: Any) -> list[str]:
            listed = await client.list_tools()
            return sorted(t.name for t in listed.tools)

        settings = (
            None
            if chain_enabled is None
            else self.settings.model_copy(update={"chain_enabled": chain_enabled})
        )
        try:
            return list(self._mcp(work, settings))
        except Exception:
            return []

    def mcp_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async def work(client: Any) -> dict[str, Any]:
            result = await client.call_tool(name, arguments)
            return {"is_error": bool(result.is_error)}

        try:
            return dict(self._mcp(work))
        except (
            Exception
        ):  # an unknown tool may raise instead of answering with an error: either way it was refused
            return {"is_error": True}

    def runner_calls(self) -> int:
        return self._counting.calls

    @contextlib.contextmanager
    def http_listener(self, token: str) -> Iterator[Listener]:
        port = _free_port()
        settings = self.settings.model_copy(
            update={"server_host": "127.0.0.1", "server_port": port, "server_token": SecretStr(token)}
        )
        with Listener(self._counting_app(settings), port) as listener:
            yield listener

    # -- reading the registry and the ledgers ---------------------------------------------------------------------------

    def _record(self, label: str) -> Any:
        name, _, version = label.partition("@")
        return self.registry.find_version(name, version)

    def status_of(self, label: str) -> str | None:
        record = self._record(label)
        return record.status if record is not None else None

    def statuses(self) -> dict[str, str]:
        return {r.label: r.status for r in self.registry.list_versions()}

    def versions_of(self, name: str) -> list[tuple[str, str]]:
        return [(r.version, r.status) for r in self.registry.list_versions(name)]

    def active_version_of(self, name: str) -> str | None:
        record = self.registry.active_version(name)
        return record.version if record is not None else None

    def gate_detail(self, label: str, gate: str) -> Any:
        record = self._record(label)
        rows = [g for g in self.registry.gate_results(record.id) if g.gate == gate] if record else []
        return rows[-1].detail if rows else None

    def gate_rows_text(self, label: str) -> str:
        record = self._record(label)
        rows = self.registry.gate_results(record.id) if record else []
        return json.dumps([[g.gate, g.status, g.detail] for g in rows], default=str)

    def latest_approval(self, label: str) -> dict[str, Any] | None:
        record = self._record(label)
        found = approvals.latest_approval(self.registry, record.id) if record else None
        if found is None or not isinstance(found.requested, dict):
            return None
        return {**found.requested, "status": found.status}

    def _read(self, folder: Path) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in sorted(folder.rglob("*.jsonl")) if folder.is_dir() else []:
            read, _problem = ledger.read_ledger(path)
            events += [{**e, "_file": path.name} for e in read]
        return events

    def events(self, label: str) -> list[dict[str, Any]]:
        return self._read(ledger.version_ledger_dir(self.home, label))

    def dev_events(self) -> list[dict[str, Any]]:
        return self._read(self.home / "ledgers" / "_dev")

    def verification_ledger(self, label: str) -> Path | None:
        found = sorted(ledger.version_ledger_dir(self.home, label).glob("verify-*.jsonl"))
        return found[-1] if found else None

    def ledger_files(self) -> list[Path]:
        folder = self.home / "ledgers"
        return sorted(folder.rglob("*.jsonl")) if folder.is_dir() else []

    def all_ledger_text(self) -> str:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in self.ledger_files())

    def text_files_holding(self, token: str) -> list[str]:
        """Files under the data directory (the registry database included) whose bytes contain `token`."""
        needle = token.encode()
        found: list[str] = []
        for path in sorted(self.home.rglob("*")):
            try:
                if path.is_file() and path.stat().st_size <= _BIG_FILE and needle in path.read_bytes():
                    found.append(str(path.relative_to(self.home)))
            except OSError:
                continue
        return found

    @staticmethod
    def sandbox_facts(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """Docker's account of the sandboxes in a ledger: how each was created, which were destroyed, what egress and decoys it saw."""
        created = [
            {
                "name": (e.get("payload") or {}).get("name"),
                "label": catalog.sandbox_label(e),
                "network_mode": ((e.get("payload") or {}).get("hardening") or {}).get("network_mode"),
            }
            for e in events
            if e.get("type") == "sandbox.created"
        ]
        types = [str(e.get("type", "")) for e in events]
        planted = next(
            (
                len((e.get("payload") or {}).get("items") or [])
                for e in events
                if e.get("type") == "canary.seeded"
            ),
            0,
        )
        return {
            "created": created,
            "destroyed": [
                (e.get("payload") or {}).get("name") for e in events if e.get("type") == "sandbox.destroyed"
            ],
            "egress_events": sum(
                1 for t in types if t in ("egress.allowed", "egress.blocked", "egress.brokered")
            ),
            "decoy_events": [
                t for t in types if t in ("canary.read", "canary.exposed", "canary.tripped", "canary.copied")
            ],
            "decoys_planted": planted,
        }

    @staticmethod
    def audit_view(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """The numbers Docker reported when the sandbox was audited, as plain values; empty when no audit is in the ledger."""
        audits = [e.get("payload") or {} for e in events if e.get("type") == "sandbox.audit"]
        view: dict[str, Any] = {}
        stats = [a["stats"] for a in audits if isinstance(a.get("stats"), dict)]
        if stats:
            s = stats[-1]
            view.update(
                {
                    k: s.get(k)
                    for k in (
                        "peak_pids",
                        "pids_limit",
                        "peak_memory_bytes",
                        "memory_limit_bytes",
                        "oom_killed",
                    )
                }
            )
        files = [a["fs"] for a in audits if isinstance(a.get("fs"), dict)]
        if files:
            fs = files[-1]
            changes, _shown, _ignored = real_rootfs_changes(
                fs.get("rootfs_changes"), fs.get("rootfs_changed"), fs.get("tmpfs")
            )
            workspace = fs.get("workspace") if isinstance(fs.get("workspace"), dict) else {}
            view.update(
                rootfs_changes=changes, workspace_bytes=workspace.get("bytes"), tmp_bytes=fs.get("tmp_bytes")
            )
        return view

    def static_findings(self, package: ToolPackage) -> list[str]:
        """The codes G1 finds in a package by reading it (nothing runs)."""
        findings, _lines, _metrics = analyse(package, self.settings)
        return sorted({f.code for f in findings})

    def blast_radius(self, name: str, version: str) -> dict[str, Any] | None:
        try:
            return blast_radius.generate(self.settings, name, version, store=False)
        except Exception:
            return None

    # -- the ledger as a record ------------------------------------------------------------------------------------------

    @staticmethod
    def verify_ledger(path: Path) -> tuple[bool, str]:
        events, problem = ledger.read_ledger(path)
        if problem:
            return False, problem
        return ledger.verify_chain(events)

    @staticmethod
    def tamper(path: Path) -> bool:
        """Change the payload of the second event on disk and leave its hash alone, as someone editing a file by hand would."""
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 3:
            return False
        event = json.loads(lines[1])
        payload = event.get("payload")
        if isinstance(payload, dict):
            payload["edited_by_hand"] = True
        else:
            event["payload"] = {"edited_by_hand": True}
        lines[1] = json.dumps(event, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    # -- cleanup and the checks every attack gets -------------------------------------------------------------------------

    def _sandbox_containers(self) -> set[str] | None:
        if self._problem is not None or self._runner_is_fake():
            return None
        try:
            import docker

            client = docker.from_env(timeout=5)
            try:
                found = client.containers.list(all=True, filters={"label": "minilocker=sandbox"})
                return {str(c.name) for c in found}
            finally:
                client.close()
        except Exception:
            return None

    def _runner_is_fake(self) -> bool:
        return self._runner is not None and type(self._runner).__name__ in ("FakeRunner", "LocalRunner")

    def remove_leftover_containers(self) -> list[str]:
        """Remove sandbox containers that did not exist when this lab started. Returns their names (an empty list means none)."""
        before = self._containers_before
        if before is None:
            return []
        removed: list[str] = []
        try:
            import docker

            client = docker.from_env(timeout=5)
            try:
                for container in client.containers.list(all=True, filters={"label": "minilocker=sandbox"}):
                    if str(container.name) not in before:
                        removed.append(str(container.name))
                        with contextlib.suppress(Exception):
                            container.remove(force=True)
            finally:
                client.close()
        except Exception:
            return removed
        return removed

    def always_checks(self, attack: Attack) -> list[CheckResult]:
        return [self._canary_check(), self._chains_check(attack), self._destroyed_check()]

    def _canary_check(self) -> CheckResult:
        label = "the host canary is intact and no host secret reached the data directory"
        try:
            intact = self.canary_path.read_text(encoding="utf-8") == self.canary_token
        except OSError:
            intact = False
        if not intact or os.environ.get(self.canary_env) != self.canary_token:
            return CheckResult(label, FAIL, "the host canary file or variable was changed", "always")
        leaked = self.text_files_holding(self.canary_token)
        if leaked:
            return CheckResult(label, FAIL, f"the canary token is in {leaked[:3]}", "always")
        return CheckResult(label, PASS, "the token appears in no file of the data directory", "always")

    def _chains_check(self, attack: Attack) -> CheckResult:
        label = "every ledger verifies as a hash chain"
        if attack.tampers_ledger:
            return CheckResult(label, SKIP, "this attack edits a ledger on purpose", "always")
        files = self.ledger_files()
        if not files:
            return CheckResult(label, SKIP, "this attack wrote no ledger", "always")
        for path in files:
            events, problem = ledger.read_ledger(path)
            if not events and not problem:
                continue
            good, message = (False, problem) if problem else ledger.verify_chain(events)
            if not good:
                return CheckResult(label, FAIL, f"{path.name}: {str(message)[:100]}", "always")
        return CheckResult(label, PASS, f"{len(files)} ledger file(s) verify", "always")

    def _destroyed_check(self) -> CheckResult:
        label = "every sandbox was destroyed"
        created, destroyed = set(), set()
        for path in self.ledger_files():
            facts = self.sandbox_facts(ledger.read_ledger(path)[0])
            created |= {c["name"] for c in facts["created"] if c["name"]}
            destroyed |= {n for n in facts["destroyed"] if n}
        left = sorted(created - destroyed) + self.leftovers
        if left:
            return CheckResult(label, FAIL, f"left behind: {', '.join(map(str, left[:3]))}", "always")
        if not created:
            return CheckResult(label, SKIP, "no sandbox was opened", "always")
        return CheckResult(label, PASS, f"{len(created)} sandbox(es) created and destroyed", "always")

    def cleanup(self) -> None:
        """Stop what the attack left running: sandboxes and the egress proxy. Removed container names are kept for the check."""
        runner = self._runner
        if runner is not None:
            stop = getattr(runner, "shutdown", None)
            if stop is not None:
                with contextlib.suppress(Exception):
                    stop()
        self.leftovers += self.remove_leftover_containers()

    def close(self) -> None:
        self.cleanup()
        for app in self._apps:
            with contextlib.suppress(Exception):
                app.close()
        with contextlib.suppress(Exception):
            self.registry.close()
        for who in self._strangers:
            with contextlib.suppress(Exception):
                who.close()
        os.environ.pop(self.canary_env, None)
        shutil.rmtree(self._canary_dir, ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)


def _ident(package: ToolPackage) -> tuple[str, str]:
    try:
        return package.manifest.name, package.manifest.version
    except Exception:
        return "?", "?"


# --- Running ---------------------------------------------------------------------------------------------------------------


def _with_deadline(fn: Callable[[], Any], seconds: float) -> Any:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # handed to the caller below, which decides what it means
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise DeadlineExceeded(f"it ran past its deadline of {seconds:g} seconds")
    if "error" in box:
        raise box["error"]
    return box["value"]


def run_one(attack: Attack, lab: Lab, *, deadline_s: float = DEADLINE_S) -> AttackResult:
    """Drive one attack and judge it. Never raises: whatever goes wrong becomes `skipped` or `inconclusive`, never `contained`."""
    started = time.monotonic()

    def done(verdict: str, evidence: str, checks: list[CheckResult] | None = None) -> AttackResult:
        return AttackResult(
            attack.id, attack.title, verdict, evidence, checks or [], time.monotonic() - started
        )

    reason = lab.unavailable_reason(attack)
    if reason is not None:
        return done(SKIPPED, reason)
    try:
        obs = _with_deadline(lambda: attack.drive(lab), deadline_s)
    except DeadlineExceeded as exc:
        lab.cleanup()
        return done(INCONCLUSIVE, str(exc))
    except SandboxUnavailable as exc:
        lab.cleanup()
        return done(SKIPPED, f"the sandbox is not available: {exc.message[:120]}")
    except SandboxError as exc:
        lab.cleanup()
        return done(INCONCLUSIVE, f"the sandbox failed during the attack: {exc.message[:120]}")
    except Exception as exc:
        lab.cleanup()
        return done(INCONCLUSIVE, f"the attack driver failed: {type(exc).__name__}: {str(exc)[:100]}")
    lab.cleanup()
    results = catalog.evaluate(attack, obs) + lab.always_checks(attack)
    return done(catalog.verdict_of(results), catalog.evidence_line(results), results)


@contextlib.contextmanager
def _home_env(home: Path) -> Iterator[None]:
    old = os.environ.get("TOOLFORGE_HOME")
    os.environ["TOOLFORGE_HOME"] = str(home)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TOOLFORGE_HOME", None)
        else:
            os.environ["TOOLFORGE_HOME"] = old


LabFactory = Callable[[Path], Lab]
Progress = Callable[[Attack, "AttackResult | None"], None]


def default_lab(home: Path) -> Lab:
    return Lab(home)


def run_attacks(
    only: Sequence[str] | None = None,
    *,
    deadline_s: float = DEADLINE_S,
    lab_factory: LabFactory = default_lab,
    progress: Progress | None = None,
) -> list[AttackResult]:
    """Run the catalogue (or the named attacks) one after another, each in its own throwaway data directory."""
    wanted = list(only) if only else list(catalog.attack_ids())
    unknown = [a for a in wanted if a not in catalog.attack_ids()]
    if unknown:
        raise ToolForgeError(
            f"no such attack: {', '.join(unknown)}",
            code="unknown_attack",
            hint="list them with `toolforge attacks list`",
        )
    logging.disable(
        logging.CRITICAL
    )  # the servers the attacks poke log every refusal; the table is the report
    try:
        return _run_each(wanted, deadline_s, lab_factory, progress)
    finally:
        logging.disable(logging.NOTSET)


def _run_each(
    wanted: list[str], deadline_s: float, lab_factory: LabFactory, progress: Progress | None
) -> list[AttackResult]:
    results: list[AttackResult] = []
    for attack in (catalog.get_attack(a) for a in wanted):
        if progress is not None:
            progress(attack, None)
        home = Path(tempfile.mkdtemp(prefix="toolforge-attack-"))
        try:
            with _home_env(home):
                lab = lab_factory(home)
                try:
                    result = run_one(attack, lab, deadline_s=deadline_s)
                finally:
                    lab.close()
        finally:
            shutil.rmtree(home, ignore_errors=True)
        results.append(result)
        if progress is not None:
            progress(attack, result)
    return results


# --- Showing the results ---------------------------------------------------------------------------------------------------


def summary_counts(results: Sequence[AttackResult]) -> dict[str, int]:
    counts = {v: 0 for v in (CONTAINED, BREACHED, INCONCLUSIVE, SKIPPED)}
    for r in results:
        counts[r.verdict] += 1
    return counts


def summary_line(results: Sequence[AttackResult]) -> str:
    c = summary_counts(results)
    return (
        f"contained {c[CONTAINED]}/{len(results)}"
        f", breached {c[BREACHED]}, inconclusive {c[INCONCLUSIVE]}, skipped {c[SKIPPED]}"
    )


def exit_code(results: Sequence[AttackResult]) -> int:
    """1 when any attack breached, 2 when none did but something is unproven or was skipped, else 0."""
    c = summary_counts(results)
    if c[BREACHED]:
        return 1
    return 2 if c[INCONCLUSIVE] or c[SKIPPED] else 0


def render_table(results: Sequence[AttackResult], *, width: int = 110) -> str:
    id_w = max([len(r.id) for r in results] + [2])
    verdict_w = max([len(r.verdict) for r in results] + [7])
    lines = [f"{'id':<{id_w}}  {'verdict':<{verdict_w}}  evidence"]
    for r in results:
        evidence = r.evidence.replace("\n", " ")
        room = max(width - id_w - verdict_w - 4, 30)
        if len(evidence) > room:
            evidence = evidence[: room - 3] + "..."
        lines.append(f"{r.id:<{id_w}}  {r.verdict:<{verdict_w}}  {evidence}")
    lines += ["", summary_line(results), FOOTER]
    return "\n".join(lines)


def report_document(results: Sequence[AttackResult], *, docker: str | None) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toolforge": __version__,
        "docker": "available" if docker is None else docker,
        "summary": summary_counts(results),
        "footer": FOOTER,
        "results": [r.as_dict() for r in results],
    }


def write_json(results: Sequence[AttackResult], directory: Path, *, docker: str | None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"attacks-{stamp}.json"
    path.write_text(
        json.dumps(report_document(results, docker=docker), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "DEADLINE_S",
    "FOOTER",
    "AttackResult",
    "DeadlineExceeded",
    "Lab",
    "Submission",
    "docker_problem",
    "exit_code",
    "render_table",
    "run_attacks",
    "run_one",
    "summary_counts",
    "summary_line",
    "write_json",
]
