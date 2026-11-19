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
