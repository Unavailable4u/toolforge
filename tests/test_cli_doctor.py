"""`toolforge doctor`, with a faked Docker client: statuses, exit codes, hints, `--clean`, and that it only ever reads."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from toolforge import cli_doctor
from toolforge.cli_doctor import Check, CheckResult, DoctorEnv, render, run_checks, run_doctor
from toolforge.config import load_settings
from toolforge.errors import SandboxUnavailable
from toolforge.sandbox import _ml_path

NOW = 1_800_000_000.0


class ImageNotFound(Exception):  # docker.errors.ImageNotFound, matched by name so the SDK is not needed
    pass


@dataclass
class FakeContainer:
    name: str
    labels: dict[str, str] = field(default_factory=lambda: {"minilocker": "sandbox"})
    age_s: float = 5000.0
    created: str | None = None
    removed: list[bool] = field(default_factory=list)
    fail_remove: bool = False

    @property
    def attrs(self) -> dict[str, str]:
        if self.created is not None:
            return {"Created": self.created}
        moment = time.gmtime(NOW - self.age_s)
        return {
            "Created": time.strftime("%Y-%m-%dT%H:%M:%S", moment) + ".123456789Z"
        }  # Docker writes nanoseconds

    def remove(self, force: bool = False) -> None:
        if self.fail_remove:
            raise RuntimeError("busy")
        self.removed.append(force)


class FakeClient:
    """Records every call so a test can prove doctor only reads."""

    def __init__(
        self,
        containers: list[FakeContainer] | None = None,
        images: dict[str, Any] | None = None,
        registry_ok: bool = True,
    ) -> None:
        self._containers = containers or []
        self._images = images if images is not None else {}
        self._registry_ok = registry_ok
        self.calls: list[str] = []
        self.containers = SimpleNamespace(
            list=self._list,
            run=self._forbidden("containers.run"),
            create=self._forbidden("containers.create"),
        )
        self.images = SimpleNamespace(
            get=self._get, get_registry_data=self._registry, pull=self._forbidden("images.pull")
        )

    def _forbidden(self, name: str):
        def refuse(*args: object, **kwargs: object) -> None:
            self.calls.append(name)
            raise AssertionError(f"doctor must not call {name}")

        return refuse

    def ping(self) -> bool:
        return True

    def version(self) -> dict[str, str]:
        return {"Version": "27.3.1"}

    def _list(self, **kwargs: object) -> list[FakeContainer]:
        self.calls.append("containers.list")
        return list(self._containers)

    def _get(self, name: str) -> Any:
        self.calls.append(f"images.get:{name}")
        if name not in self._images:
            raise ImageNotFound(name)
        return self._images[name]

    def _registry(self, name: str) -> Any:
        self.calls.append(f"images.get_registry_data:{name}")
        if not self._registry_ok:
            raise OSError("offline")
        return object()


def image(digest: str = "sha256:" + "ab" * 32) -> SimpleNamespace:
    return SimpleNamespace(attrs={"RepoDigests": [f"python@{digest}"]}, id="sha256:" + "cd" * 32)


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A MiniLocker `backend` at the pinned commit, so the minilocker check is green."""
    backend = tmp_path / "ml" / "backend"
    (backend / "minilocker").mkdir(parents=True)
    (backend / "minilocker" / "__init__.py").write_text("")
    monkeypatch.setenv("TOOLFORGE_MINILOCKER_PATH", str(backend))
    monkeypatch.setenv("TOOLFORGE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(_ml_path, "checkout_commit", lambda b: _ml_path.PINNED_COMMIT)
    return backend


def env_for(client: FakeClient | None, clean: bool = False) -> DoctorEnv:
    factory = (lambda: client) if client is not None else _raise_unavailable
    return DoctorEnv(settings=load_settings(), clean=clean, client_factory=factory, now=lambda: NOW)


def _raise_unavailable() -> Any:
    raise OSError("no such socket")


def by_name(results: list[tuple[Check, CheckResult]]) -> dict[str, CheckResult]:
    return {check.name: result for check, result in results}


# -- the happy path ------------------------------------------------------------------------------------------------------------


def test_everything_ready_exits_zero(pinned: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient(images={"python:3.12-slim": image()})
    assert run_doctor(env_for(client)) == 0
    out = capsys.readouterr().out
    assert "[ ok ] python" in out and "engine 27.3.1" in out and "sha256:" + "ab" * 6 in out
    assert "pinned commit " + _ml_path.PINNED_COMMIT[:12] in out and "0 failed" in out


def test_doctor_only_reads(pinned: Path) -> None:
    client = FakeClient(containers=[FakeContainer("minilocker-old")], images={})
    run_doctor(env_for(client))  # no --clean: nothing is removed, started or pulled
    assert not any(call in {"containers.run", "containers.create", "images.pull"} for call in client.calls)
    assert client._containers[0].removed == []  # listed, reported, left in place


# -- docker --------------------------------------------------------------------------------------------------------------------


def test_unreachable_docker_fails_and_dependent_checks_are_skipped(pinned: Path) -> None:
    results = by_name(run_checks(env_for(None)))
    assert results["docker daemon"].status == "fail" and "Docker" in (results["docker daemon"].hint or "")
    for name in ("runtime image", "egress proxy image", "leftover containers"):
        assert results[name].status == "skip", name
    assert run_doctor(env_for(None)) == 1


def test_a_missing_sdk_says_how_to_install_it(pinned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_sdk() -> Any:
        raise SandboxUnavailable(
            'the Python package "docker" is not installed',
            hint='install it with `pip install -e ".[sandbox]"`',
        )

    env = DoctorEnv(settings=load_settings(), client_factory=no_sdk, now=lambda: NOW)
    result = by_name(run_checks(env))["docker daemon"]
    assert result.status == "fail" and ".[sandbox]" in (result.hint or "")


def test_the_docker_failure_is_asked_once_and_remembered(pinned: Path) -> None:
    calls: list[int] = []

    def factory() -> Any:
        calls.append(1)
        raise OSError("down")

    env = DoctorEnv(settings=load_settings(), client_factory=factory, now=lambda: NOW)
    run_checks(env)
    assert len(calls) == 1


# -- python and the data directory ---------------------------------------------------------------------------------------------


def test_an_old_python_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_doctor, "sys", SimpleNamespace(version_info=SimpleNamespace(major=3, minor=11, micro=9))
    )
    result = cli_doctor.check_python(env_for(None))
    assert result.status == "fail" and "3.12" in result.message


def test_data_directory_is_ok_when_it_can_be_created_later(pinned: Path) -> None:
    result = cli_doctor.check_data_dir(env_for(None))
    assert result.status == "ok" and "first use" in result.message


def test_an_unwritable_data_directory_fails_with_a_hint(
    pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_doctor.os, "access", lambda *a, **k: False)
    result = cli_doctor.check_data_dir(env_for(None))
    assert result.status == "fail" and "TOOLFORGE_HOME" in (result.hint or "")


def test_a_data_directory_under_a_file_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("TOOLFORGE_HOME", str(blocker / "sub"))
    assert cli_doctor.check_data_dir(env_for(None)).status == "fail"


# -- the runtime and proxy images ------------------------------------------------------------------------------------------------


def test_a_missing_runtime_image_is_a_warning_with_the_pull_command(pinned: Path) -> None:
    result = cli_doctor.check_runtime_image(env_for(FakeClient(images={})))
    assert (
        result.status == "warn"
        and "docker pull python:3.12-slim" in (result.hint or "")
        and "internet" in result.message
    )


def test_the_runtime_image_setting_is_honoured(pinned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_RUNTIME_IMAGE", "registry.example/tf:1")
    client = FakeClient(images={"registry.example/tf:1": image()})
    result = cli_doctor.check_runtime_image(env_for(client))
    assert result.status == "ok" and "registry.example/tf:1" in result.message


def test_an_image_without_a_repo_digest_shows_its_id(pinned: Path) -> None:
    local = SimpleNamespace(attrs={"RepoDigests": []}, id="sha256:" + "ef" * 32)
    result = cli_doctor.check_runtime_image(env_for(FakeClient(images={"python:3.12-slim": local})))
    assert result.status == "ok" and "sha256:" + "ef" * 6 in result.message


def test_proxy_image_present_pullable_or_offline(pinned: Path) -> None:
    name = cli_doctor.PROXY_IMAGE
    assert cli_doctor.check_proxy_image(env_for(FakeClient(images={name: image()}))).status == "ok"
    pullable = cli_doctor.check_proxy_image(env_for(FakeClient(images={}, registry_ok=True)))
    assert pullable.status == "ok" and "can be pulled" in pullable.message
    offline = cli_doctor.check_proxy_image(env_for(FakeClient(images={}, registry_ok=False)))
    assert offline.status == "skip" and "offline" in offline.message  # skipped, never a failure


# -- minilocker ----------------------------------------------------------------------------------------------------------------


def test_missing_minilocker_fails_with_the_three_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_ml_path, "repo_root", lambda: tmp_path)
    result = cli_doctor.check_minilocker(env_for(None))
    assert result.status == "fail"
    for command in _ml_path.ADD_COMMANDS:
        assert command in (result.hint or "")


def test_the_wrong_commit_fails_and_names_both(pinned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ml_path, "checkout_commit", lambda b: "1" * 40)
    result = cli_doctor.check_minilocker(env_for(None))
    assert (
        result.status == "fail"
        and "111111111111" in result.message
        and _ml_path.PINNED_COMMIT[:12] in result.message
    )
    assert f"checkout {_ml_path.PINNED_COMMIT}" in (result.hint or "")


def test_an_unreadable_commit_is_a_warning_not_a_pass(pinned: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ml_path, "checkout_commit", lambda b: None)
    assert cli_doctor.check_minilocker(env_for(None)).status == "warn"


# -- leftover containers and --clean ---------------------------------------------------------------------------------------------


def test_old_sandbox_containers_are_a_warning_and_stay_without_clean(pinned: Path) -> None:
    old, young = FakeContainer("minilocker-aaa", age_s=3600), FakeContainer("minilocker-bbb", age_s=30)
    result = cli_doctor.check_leftovers(env_for(FakeClient(containers=[old, young])))
    assert (
        result.status == "warn"
        and "minilocker-aaa" in result.message
        and "minilocker-bbb" not in result.message
    )
    assert "--clean" in (result.hint or "") and old.removed == [] and young.removed == []


def test_clean_removes_only_old_labelled_sandboxes(pinned: Path) -> None:
    old = FakeContainer("minilocker-aaa", age_s=3600)
    young = FakeContainer("minilocker-bbb", age_s=30)
    proxy = FakeContainer("minilocker-egress", labels={"minilocker": "egress"}, age_s=99999)
    session = FakeContainer("minilocker-session", labels={}, age_s=99999)  # P13's, not ours to remove
    unrelated = FakeContainer("postgres", labels={"minilocker": "sandbox"}, age_s=99999)  # wrong name prefix
    result = cli_doctor.check_leftovers(
        env_for(FakeClient(containers=[old, young, proxy, session, unrelated]), clean=True)
    )
    assert result.status == "ok" and "removed 1" in result.message and "minilocker-aaa" in result.message
    assert old.removed == [True]  # forced
    assert (young.removed, proxy.removed, session.removed, unrelated.removed) == ([], [], [], [])
    assert "left alone" in result.message


def test_a_container_that_cannot_be_removed_is_reported_not_hidden(pinned: Path) -> None:
    stuck = FakeContainer("minilocker-stuck", age_s=3600, fail_remove=True)
    result = cli_doctor.check_leftovers(env_for(FakeClient(containers=[stuck]), clean=True))
    assert (
        result.status == "warn"
        and "minilocker-stuck" in result.message
        and "docker rm -f" in (result.hint or "")
    )


def test_a_container_with_an_unreadable_age_is_not_treated_as_a_leftover(pinned: Path) -> None:
    odd = FakeContainer("minilocker-odd", created="not a date")
    assert cli_doctor.check_leftovers(env_for(FakeClient(containers=[odd]), clean=True)).status == "ok"
    assert odd.removed == []


def test_no_leftovers_is_ok(pinned: Path) -> None:
    assert cli_doctor.check_leftovers(env_for(FakeClient())).message == "no leftover sandbox containers"


# -- running and rendering -------------------------------------------------------------------------------------------------------


def test_a_check_that_crashes_is_a_failure_not_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    def boom(env: DoctorEnv) -> CheckResult:
        raise KeyError("x")

    assert run_doctor(env_for(None), [Check("boom", True, boom)]) == 1
    assert "[FAIL] boom" in capsys.readouterr().out


def test_an_optional_check_that_fails_is_only_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    fails = Check("nice to have", False, lambda env: CheckResult("fail", "missing"))
    assert run_doctor(env_for(None), [fails]) == 0
    assert "[WARN] nice to have" in capsys.readouterr().out


def test_the_checks_list_is_the_one_place_to_add_a_check() -> None:
    names = [check.name for check in cli_doctor.CHECKS]
    assert names == [
        "python",
        "docker daemon",
        "runtime image",
        "minilocker",
        "egress proxy image",
        "leftover containers",
        "data directory",
        "challenger model",
        "builder model",
    ]
    required = {check.name for check in cli_doctor.CHECKS if check.required}
    assert required == {"python", "docker daemon", "minilocker", "data directory"}


def test_output_from_docker_cannot_inject_control_characters() -> None:
    hostile = CheckResult("warn", "container minilocker-\x1b[31mred\x07 is old", "run\x1b]0;title\x07 it")
    out = render([(Check("x", False, lambda e: hostile), hostile)])
    assert "\x1b" not in out and "\x07" not in out and "[WARN] x" in out


def test_hints_are_aligned_and_multiline_hints_keep_their_lines() -> None:
    result = CheckResult("fail", "gone", "line one\nline two")
    out = render([(Check("minilocker", True, lambda e: result), result)]).splitlines()
    assert out[1].endswith("hint: line one") and out[2].endswith("line two") and out[2].count("hint:") == 0
    assert "1 failed" in out[-2] and "fix the [FAIL] lines" in out[-1]


def test_the_command_is_wired_to_main_with_a_clean_flag(
    pinned: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from toolforge.cli import main

    seen: list[bool] = []
    monkeypatch.setattr(cli_doctor, "_docker_client", lambda: FakeClient())
    monkeypatch.setattr(cli_doctor, "run_doctor", lambda env, checks=None: seen.append(env.clean) or 0)
    assert main(["doctor"]) == 0 and main(["doctor", "--clean"]) == 0
    assert seen == [False, True]


# -- the challenger model (P10) ----------------------------------------------------------------------------------------------


def challenger_result(monkeypatch: pytest.MonkeyPatch, **env: str) -> CheckResult:
    for name, value in env.items():
        monkeypatch.setenv(f"TOOLFORGE_{name.upper()}", value)
    check = next(c for c in cli_doctor.CHECKS if c.name == "challenger model")
    return check.run(env_for(None))


def test_no_challenger_is_a_skip_that_says_what_it_means(monkeypatch: pytest.MonkeyPatch) -> None:
    result = challenger_result(monkeypatch)
    assert (
        result.status == "skip"
        and "G5 is skipped" in result.message
        and "TOOLFORGE_CHALLENGER_BASE_URL" in (result.hint or "")
    )


def test_a_half_configured_challenger_is_a_warning_naming_the_missing_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = challenger_result(monkeypatch, llm_base_url="https://llm.test/v1")
    assert result.status == "warn" and "TOOLFORGE_CHALLENGER_MODEL" in result.message


def test_the_challenger_model_equal_to_the_builder_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    result = challenger_result(
        monkeypatch, llm_base_url="https://llm.test/v1", llm_model="same-model", challenger_model="Same-Model"
    )
    assert (
        result.status == "warn" and "weaker evidence" in result.message and "different" in (result.hint or "")
    )
    fallback = challenger_result(monkeypatch, llm_base_url="https://llm.test/v1", llm_model="same-model")
    assert fallback.status == "warn"  # no challenger of its own: it is the builder


def test_a_different_challenger_is_ok_and_the_key_and_credentials_are_never_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = challenger_result(
        monkeypatch,
        llm_base_url="https://llm.test/v1",
        llm_model="builder-m",
        challenger_model="other-m",
        challenger_api_key="sk-very-secret-value",
    )
    assert (
        result.status == "ok"
        and "other-m at llm.test" in result.message
        and "with an API key" in result.message
    )
    assert "sk-very-secret-value" not in result.message + (result.hint or "")


def test_an_unusable_base_url_is_a_warning_and_never_echoes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = challenger_result(
        monkeypatch, challenger_base_url="https://user:hunter2@llm.test/v1", challenger_model="m"
    )
    assert result.status == "warn" and "hunter2" not in result.message + (result.hint or "")
    assert (
        challenger_result(monkeypatch, challenger_base_url="ftp://llm.test", challenger_model="m").status
        == "warn"
    )


def test_doctor_makes_no_model_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("doctor must not contact the model provider")

    monkeypatch.setattr("toolforge.llm.client.UrllibTransport.post", forbidden)
    assert (
        challenger_result(
            monkeypatch, llm_base_url="https://llm.test/v1", llm_model="m", challenger_model="n"
        ).status
        == "ok"
    )


# -- the builder model (P11) ---------------------------------------------------------------------------------------------------


def builder_result(monkeypatch: pytest.MonkeyPatch, **env: str) -> CheckResult:
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(f"TOOLFORGE_{name}", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(f"TOOLFORGE_{name.upper()}", value)
    check = next(c for c in cli_doctor.CHECKS if c.name == "builder model")
    assert not check.required  # a missing model never makes the machine "not ready"
    return check.run(env_for(None))


def test_no_builder_model_is_a_skip_that_says_only_reuse_works(monkeypatch: pytest.MonkeyPatch) -> None:
    result = builder_result(monkeypatch)
    assert (
        result.status == "skip" and "only reuse" in result.message and "TOOLFORGE_LLM_BASE_URL" in result.hint
    )


def test_a_half_configured_builder_is_a_warning_naming_the_missing_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only_url = builder_result(monkeypatch, llm_base_url="https://llm.test/v1")
    assert only_url.status == "warn" and "TOOLFORGE_LLM_MODEL" in only_url.message + only_url.hint
    only_model = builder_result(monkeypatch, llm_model="m")
    assert only_model.status == "warn" and "TOOLFORGE_LLM_BASE_URL" in only_model.message + only_model.hint


def test_a_usable_builder_is_ok_shows_host_limits_and_never_the_key_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = builder_result(
        monkeypatch,
        llm_base_url="https://llm.test/v1",
        llm_model="the-model",
        llm_api_key="sk-very-secret-value",
    )
    assert result.status == "ok" and "the-model" in result.message and "llm.test" in result.message
    assert (
        "with an API key" in result.message
        and "4 rounds" in result.message
        and "60000 tokens" in result.message
    )
    assert "sk-very-secret-value" not in result.message + (result.hint or "") and "/v1" not in result.message
    keyless = builder_result(monkeypatch, llm_base_url="http://localhost:11434/v1", llm_model="local")
    assert keyless.status == "ok" and "without an API key" in keyless.message


def test_a_builder_base_url_with_credentials_or_the_wrong_scheme_is_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_login = builder_result(monkeypatch, llm_base_url="https://user:hunter2@llm.test/v1", llm_model="m")
    assert with_login.status == "warn" and "hunter2" not in with_login.message + (with_login.hint or "")
    assert builder_result(monkeypatch, llm_base_url="ftp://llm.test", llm_model="m").status == "warn"


# -- containers of stateful sessions (P13) -------------------------------------------------------------------------------------


def _open_session(box: str, *, heartbeat_age_s: float) -> str:
    """A session row in the registry doctor opens (TOOLFORGE_HOME), whose owner last reported `heartbeat_age_s` ago."""
    import copy

    from conftest import BASE_FILES, BASE_MANIFEST

    from toolforge.models.package import ToolPackage
    from toolforge.registry import Registry
    from toolforge.registry.sessions import new_session_id

    with Registry.from_settings(load_settings()) as registry:
        manifest = copy.deepcopy(BASE_MANIFEST)
        manifest["version"] = (
            f"1.0.{len(registry.list_sessions())}"  # one version per session: versions are immutable
        )
        version = registry.add_version(ToolPackage(manifest, dict(BASE_FILES)), created_by="test")
        _, public, digest = new_session_id()
        registry.open_session(
            public_id=public,
            secret_sha256=digest,
            version_id=version.id,
            label="counter 0.1.0",
            owner="o",
            box=box,
            now=NOW - heartbeat_age_s,
            ttl_s=900,
            max_s=3600,
        )
    return public


def _session_state(public: str) -> tuple[str, str | None]:
    from toolforge.registry import Registry

    with Registry.from_settings(load_settings()) as registry:
        row = registry.get_session(public)
        assert row is not None
        return row.state, row.reason


def test_the_container_of_a_live_session_is_left_alone_however_old_it_is(pinned: Path) -> None:
    box = FakeContainer("minilocker-live", age_s=3000)
    _open_session("minilocker-live", heartbeat_age_s=3)
    result = cli_doctor.check_leftovers(env_for(FakeClient(containers=[box]), clean=True))
    assert result.status == "ok" and box.removed == [] and "1 in use by open sessions" in result.message


def test_without_clean_a_session_container_is_not_reported_as_a_leak(pinned: Path) -> None:
    box = FakeContainer("minilocker-live", age_s=3000)
    _open_session("minilocker-live", heartbeat_age_s=3)
    result = cli_doctor.check_leftovers(env_for(FakeClient(containers=[box])))
    assert result.status == "ok" and "older than" not in result.message


def test_the_container_of_a_session_whose_server_is_gone_is_a_leak_even_when_young(pinned: Path) -> None:
    box = FakeContainer("minilocker-orphan", age_s=20)
    _open_session("minilocker-orphan", heartbeat_age_s=300)
    warn = cli_doctor.check_leftovers(env_for(FakeClient(containers=[box])))
    assert warn.status == "warn" and "minilocker-orphan" in warn.message and box.removed == []


def test_clean_removes_an_orphaned_session_container_and_marks_the_session_lost(pinned: Path) -> None:
    orphan = FakeContainer("minilocker-orphan", age_s=20)
    live = FakeContainer("minilocker-live", age_s=4000)
    gone = _open_session("minilocker-orphan", heartbeat_age_s=300)
    alive = _open_session("minilocker-live", heartbeat_age_s=2)
    result = cli_doctor.check_leftovers(env_for(FakeClient(containers=[orphan, live]), clean=True))
    assert result.status == "ok" and orphan.removed == [True] and live.removed == []
    state, reason = _session_state(gone)
    assert state == "lost" and "doctor" in (reason or "")
    assert _session_state(alive)[0] == "open"


def test_clean_marks_an_orphaned_session_lost_even_when_its_container_is_already_gone(pinned: Path) -> None:
    gone = _open_session("minilocker-vanished", heartbeat_age_s=300)
    cli_doctor.check_leftovers(env_for(FakeClient(containers=[]), clean=True))
    assert _session_state(gone)[0] == "lost"


def test_doctor_without_a_registry_still_works(pinned: Path) -> None:
    old = FakeContainer("minilocker-aaa", age_s=3600)
    assert cli_doctor.check_leftovers(env_for(FakeClient(containers=[old]))).status == "warn"
