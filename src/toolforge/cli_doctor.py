"""`toolforge doctor`: is this machine ready to run tools in a sandbox, and what is missing if not.

Each check is a small function registered in `CHECKS`; later patches append theirs (P10 adds the "challenger model equals builder
model" warning, P11 the builder model check, P13 makes `--clean` also remove session containers) without editing a long function.
A check returns a `CheckResult`; one marked `required` that fails makes the exit code 1, an optional one that fails is shown as a
warning. A check that raises is reported as failed, never as a traceback.

Doctor only reads, except for `--clean`, which removes containers labelled `minilocker=sandbox` that are older than 10 minutes.
It never starts a container and never pulls an image. Proven by `tests/test_cli_doctor.py`, which uses a faked Docker client.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from toolforge import textsafety
from toolforge.config import Settings, load_settings
from toolforge.errors import SandboxUnavailable
from toolforge.sandbox import _ml_path

Status = Literal["ok", "warn", "fail", "skip"]

MIN_PYTHON = (3, 12)
LEFTOVER_AFTER_S = 600  # a sandbox container older than this is a leftover, not a run in progress
PROXY_IMAGE = "python:3.12-slim"  # the image MiniLocker's EgressManager starts the proxy from by default
SANDBOX_LABEL = "minilocker=sandbox"


@dataclass(frozen=True)
class CheckResult:
    status: Status
    message: str
    hint: str | None = None


@dataclass
class DoctorEnv:
    """What the checks look at. `client_factory` lets a test hand in a fake Docker client."""

    settings: Settings
    clean: bool = False
    client_factory: Callable[[], Any] | None = None
    now: Callable[[], float] = time.time
    _client: Any = None
    _client_error: SandboxUnavailable | None = None

    def client(self) -> Any:
        """The Docker client, once; raises `SandboxUnavailable` (also on every later call) when the daemon cannot be reached."""
        if self._client_error is not None:
            raise self._client_error
        if self._client is None:
            try:
                client = (self.client_factory or _docker_client)()
                client.ping()
            except SandboxUnavailable as exc:
                self._client_error = exc
                raise
            except Exception as exc:
                self._client_error = SandboxUnavailable(
                    f"cannot reach the Docker daemon: {type(exc).__name__}: {str(exc)[:160]}",
                    hint="start Docker (on WSL2: Docker Desktop with WSL integration, see docs/guide/06-wsl2-setup.md)",
                )
                raise self._client_error from exc
            self._client = client
        return self._client


@dataclass(frozen=True)
class Check:
    name: str
    required: bool
    run: Callable[[DoctorEnv], CheckResult]


def _docker_client() -> Any:
    try:
        import docker
    except ImportError as exc:
        raise SandboxUnavailable(
            'the Python package "docker" is not installed',
            hint='install it with `pip install -e ".[sandbox]"`',
        ) from exc
    return docker.from_env(timeout=5)


def _short(digest: str) -> str:
    return digest[:19]  # "sha256:" and twelve hex digits


def _image_digest(image: Any) -> str:
    attrs = getattr(image, "attrs", None) or {}
    for reference in attrs.get("RepoDigests") or []:
        _, _, digest = str(reference).partition("@")
        if digest:
            return _short(digest)
    identifier = str(getattr(image, "id", "") or attrs.get("Id", ""))
    return _short(identifier) if identifier else "digest unknown"


def check_python(env: DoctorEnv) -> CheckResult:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= MIN_PYTHON:
        return CheckResult("ok", f"Python {text}")
    return CheckResult("fail", f"Python {text}; ToolForge needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")


def check_docker(env: DoctorEnv) -> CheckResult:
    try:
        client = env.client()
    except SandboxUnavailable as exc:
        return CheckResult("fail", exc.message, exc.hint)
    try:
        engine = str((client.version() or {}).get("Version", "")).strip()
    except Exception:
        engine = ""
    return CheckResult("ok", f"Docker daemon reachable{f' (engine {engine})' if engine else ''}")


def check_runtime_image(env: DoctorEnv) -> CheckResult:
    image = env.settings.runtime_image
    try:
        client = env.client()
    except SandboxUnavailable:
        return CheckResult("skip", "needs a reachable Docker daemon")
    try:
        found = client.images.get(image)
    except Exception as exc:
        if type(exc).__name__ == "ImageNotFound":
            return CheckResult(
                "warn",
                f"{image} is not present locally; the first sandbox run pulls it (needs internet once)",
                f"or pull it now: docker pull {image}",
            )
        return CheckResult("warn", f"could not look up {image}: {type(exc).__name__}")
    return CheckResult("ok", f"{image} present ({_image_digest(found)})")


def check_minilocker(env: DoctorEnv) -> CheckResult:
    backend = _ml_path.backend_dir(env.settings)
    overridden = env.settings.minilocker_path is not None
    if not (backend / "minilocker" / "__init__.py").is_file():
        return CheckResult(
            "fail", f"MiniLocker is missing at {backend}", _ml_path.advice(backend, overridden)
        )
    pin = _ml_path.PINNED_COMMIT
    commit = _ml_path.checkout_commit(backend)
    if commit is None:
        return CheckResult(
            "warn",
            f"MiniLocker found at {backend}, but its git commit could not be read; the pin is {pin[:12]}",
            "use the submodule (unset TOOLFORGE_MINILOCKER_PATH) so the pin can be checked",
        )
    if commit != pin:
        return CheckResult(
            "fail",
            f"MiniLocker is at commit {commit[:12]} but the pin is {pin[:12]}",
            f"git -C {_ml_path.SUBMODULE_PATH} checkout {pin}   (or: {_ml_path.INIT_COMMAND})",
        )
    return CheckResult("ok", f"MiniLocker present at the pinned commit {pin[:12]}")


def check_proxy_image(env: DoctorEnv) -> CheckResult:
    try:
        client = env.client()
    except SandboxUnavailable:
        return CheckResult("skip", "needs a reachable Docker daemon")
    try:
        client.images.get(PROXY_IMAGE)
        return CheckResult("ok", f"{PROXY_IMAGE} (egress proxy) present")
    except Exception as exc:
        if type(exc).__name__ != "ImageNotFound":
            return CheckResult("skip", f"could not look up {PROXY_IMAGE}: {type(exc).__name__}")
    try:
        client.images.get_registry_data(PROXY_IMAGE)
    except Exception:
        return CheckResult(
            "skip",
            f"{PROXY_IMAGE} (egress proxy) is not present and the registry was not reachable: offline?",
        )
    return CheckResult("ok", f"{PROXY_IMAGE} (egress proxy) is not present but can be pulled")


def _age_seconds(container: Any, now: float) -> float | None:
    created = str((getattr(container, "attrs", None) or {}).get("Created", ""))
    if not created:
        return None
    try:
        # Docker writes nanoseconds ("2026-11-19T08:00:00.123456789Z"); datetime wants at most microseconds.
        stamp = created.rstrip("Z")
        if "." in stamp:
            head, fraction = stamp.split(".", 1)
            stamp = f"{head}.{fraction[:6]}"
        moment = datetime.fromisoformat(stamp + "+00:00")
    except ValueError:
        return None
    return max(now - moment.timestamp(), 0.0)


def check_leftovers(env: DoctorEnv) -> CheckResult:
    try:
        client = env.client()
    except SandboxUnavailable:
        return CheckResult("skip", "needs a reachable Docker daemon")
    try:
        found = [
            c
            for c in client.containers.list(all=True, filters={"name": "minilocker-"})
            if str(c.name).startswith("minilocker-")
        ]
    except Exception as exc:
        return CheckResult("warn", f"could not list containers: {type(exc).__name__}")
    now = env.now()
    sandboxes = [c for c in found if (getattr(c, "labels", None) or {}).get("minilocker") == "sandbox"]
    others = [c for c in found if c not in sandboxes]
    old = [c for c in sandboxes if (_age_seconds(c, now) or 0) > LEFTOVER_AFTER_S]
    young = [c for c in sandboxes if c not in old]
    note = f"; left alone: {', '.join(str(c.name) for c in others)}" if others else ""
    if old and env.clean:
        removed, failed = [], []
        for container in old:
            try:
                container.remove(force=True)
                removed.append(str(container.name))
            except Exception:
                failed.append(str(container.name))
        if failed:
            return CheckResult(
                "warn",
                f"removed {len(removed)}, could not remove: {', '.join(failed)}",
                "try `docker rm -f` on them",
            )
        return CheckResult(
            "ok", f"removed {len(removed)} leftover sandbox container(s): {', '.join(removed)}{note}"
        )
    if old:
        names = ", ".join(str(c.name) for c in old)
        return CheckResult(
            "warn",
            f"{len(old)} sandbox container(s) older than {LEFTOVER_AFTER_S // 60} minutes: {names}{note}",
            "remove them with `toolforge doctor --clean`",
        )
    if young:
        return CheckResult(
            "ok",
            f"{len(young)} sandbox container(s) younger than {LEFTOVER_AFTER_S // 60} minutes (in use, left alone){note}",
        )
    return CheckResult("ok", f"no leftover sandbox containers{note}")


def check_data_dir(env: DoctorEnv) -> CheckResult:
    home = env.settings.home
    probe = home
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        return CheckResult("fail", f"{probe} is not a directory, so {home} cannot be created")
    if not os.access(probe, os.W_OK | os.X_OK):
        return CheckResult(
            "fail",
            f"{probe} is not writable, so {home} cannot be used",
            "set TOOLFORGE_HOME to a writable directory",
        )
    suffix = "" if probe == home else " (does not exist yet; it is created on first use)"
    return CheckResult("ok", f"data directory {home} is writable{suffix}")


def check_challenger(env: DoctorEnv) -> CheckResult:
    """Is gate G5 able to run, and is the challenger a different model from the builder? Reads settings only: no request is made."""
    from urllib.parse import urlsplit

    settings = env.settings
    base_url = settings.effective_challenger_base_url
    model = settings.effective_challenger_model
    if not base_url and not model:
        return CheckResult(
            "skip",
            "no challenger model is configured, so gate G5 is skipped and tools stop at verified_basic",
            "set TOOLFORGE_CHALLENGER_BASE_URL and TOOLFORGE_CHALLENGER_MODEL (or the TOOLFORGE_LLM_* values); see docs/verification.md",
        )
    if not base_url or not model:
        missing = "TOOLFORGE_CHALLENGER_BASE_URL" if not base_url else "TOOLFORGE_CHALLENGER_MODEL"
        return CheckResult(
            "warn",
            f"the challenger model is only partly configured ({missing} is missing), so gate G5 is skipped",
            f"set {missing} (or the matching TOOLFORGE_LLM_* value)",
        )
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return CheckResult(
            "warn",
            "the challenger base URL is not usable, so gate G5 is skipped",
            "it must be an http or https address, without a user name or password in it",
        )
    where = f"{model} at {parts.hostname}"
    if settings.challenger_shares_builder_model:
        return CheckResult(
            "warn",
            f"the challenger uses the builder's model ({where}): G5 is weaker evidence, because both tend to miss the same things",
            "use a different model family, or at least a different size, for TOOLFORGE_CHALLENGER_MODEL",
        )
    key = "with an API key" if settings.effective_challenger_api_key is not None else "without an API key"
    return CheckResult(
        "ok", f"challenger model {where}, {key} (not contacted: doctor makes no model requests)"
    )


def check_builder(env: DoctorEnv) -> CheckResult:
    """Can `forge_build` and `toolforge build` ask a model for a package? Reads settings only: no request is made."""
    from urllib.parse import urlsplit

    settings = env.settings
    base_url, model = settings.llm_base_url, settings.llm_model
    if not base_url and not model:
        return CheckResult(
            "skip",
            "no builder model is configured, so `toolforge build` and forge_build can only reuse tools that already exist",
            "set TOOLFORGE_LLM_BASE_URL and TOOLFORGE_LLM_MODEL (and TOOLFORGE_LLM_API_KEY if the endpoint needs one)",
        )
    if not base_url or not model:
        missing = "TOOLFORGE_LLM_BASE_URL" if not base_url else "TOOLFORGE_LLM_MODEL"
        return CheckResult(
            "warn",
            f"the builder model is only partly configured ({missing} is missing), so nothing can be built",
            f"set {missing}",
        )
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return CheckResult(
            "warn",
            "the builder base URL is not usable, so nothing can be built",
            "it must be an http or https address, without a user name or password in it",
        )
    key = "with an API key" if settings.llm_api_key is not None else "without an API key"
    return CheckResult(
        "ok",
        f"builder model {model} at {parts.hostname}, {key}; at most {settings.builder_max_rounds} rounds and "
        f"{settings.builder_token_budget} tokens a build (not contacted: doctor makes no model requests)",
    )


# One list, in the order they are printed. Append to it to add a check; a required check that fails makes `doctor` exit 1.
CHECKS: list[Check] = [
    Check("python", True, check_python),
    Check("docker daemon", True, check_docker),
    Check("runtime image", False, check_runtime_image),
    Check("minilocker", True, check_minilocker),
    Check("egress proxy image", False, check_proxy_image),
    Check("leftover containers", False, check_leftovers),
    Check("data directory", True, check_data_dir),
    Check("challenger model", False, check_challenger),
    Check("builder model", False, check_builder),
]


def run_checks(env: DoctorEnv, checks: Sequence[Check] | None = None) -> list[tuple[Check, CheckResult]]:
    results = []
    for check in CHECKS if checks is None else checks:
        try:
            result = check.run(env)
        except Exception as exc:
            result = CheckResult("fail", f"the check crashed: {type(exc).__name__}: {str(exc)[:160]}")
        if result.status == "fail" and not check.required:
            result = replace(result, status="warn")
        results.append((check, result))
    return results


_LABELS = {"ok": "[ ok ]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[skip]"}


def _plain(text: str, limit: int = 400) -> str:
    return textsafety.sanitize(text, max_chars=limit).replace("\n", " ")


def render(results: Sequence[tuple[Check, CheckResult]]) -> str:
    width = max((len(check.name) for check, _ in results), default=0)
    lines = []
    for check, result in results:
        lines.append(f"{_LABELS[result.status]} {check.name.ljust(width)}  {_plain(result.message)}")
        if result.hint:
            hint_lines = textsafety.sanitize(result.hint, max_chars=800).splitlines() or [""]
            pad = " " * (len(_LABELS["ok"]) + 1 + width + 2)  # lines up under the message
            lines.append(f"{pad}hint: {hint_lines[0]}")
            lines.extend(f"{pad}      {line}" for line in hint_lines[1:])
    counts = {status: sum(1 for _, r in results if r.status == status) for status in _LABELS}
    lines.append(
        f"{len(results)} checks: {counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failed, {counts['skip']} skipped"
    )
    if counts["fail"]:
        lines.append(
            "doctor found problems that stop tools from running in a sandbox; fix the [FAIL] lines above."
        )
    return "\n".join(lines)


def run_doctor(env: DoctorEnv, checks: Sequence[Check] | None = None) -> int:
    results = run_checks(env, checks)
    print(render(results))
    return 1 if any(result.status == "fail" for _, result in results) else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    return run_doctor(DoctorEnv(settings=load_settings(), clean=bool(args.clean)))


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctor", help="check that Docker, MiniLocker and the data directory are ready"
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove sandbox containers left behind by a crash (labelled minilocker=sandbox, older than 10 minutes)",
    )
    parser.set_defaults(handler=_cmd_doctor)
