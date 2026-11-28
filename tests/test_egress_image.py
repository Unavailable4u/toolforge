"""`TOOLFORGE_EGRESS_PROXY_IMAGE`: the egress proxy runs MiniLocker's packaged module from an image instead of bind-mounting a file.

Run from a container, a bind mount of this process's own file would name a path that does not exist on the Docker host (docker-compose.yml, docs/clients.md).
With the setting empty nothing changes. A stub stands in for MiniLocker, so no Docker is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from toolforge.config import Settings
from toolforge.errors import SandboxUnavailable
from toolforge.sandbox import minilocker_runner
from toolforge.sandbox.minilocker_runner import MiniLockerRunner, _start_proxy


class StubManager:
    seen: list[dict[str, Any]] = []

    def __init__(self, hosts: list[str], **kwargs: Any) -> None:
        self.seen.append({"hosts": hosts, **kwargs})

    def start(self) -> StubManager:
        return self


@pytest.fixture(autouse=True)
def _stub_minilocker(monkeypatch: pytest.MonkeyPatch) -> None:
    StubManager.seen = []
    module = SimpleNamespace(EgressManager=StubManager)
    monkeypatch.setattr(minilocker_runner._ml_path, "load", lambda name, settings=None: module)


def test_default_start_keeps_the_bind_mount_arguments() -> None:
    _start_proxy(["a.example.com"], {"svc": {"host": "h"}})
    assert StubManager.seen == [{"hosts": ["a.example.com"], "broker": {"svc": {"host": "h"}}}]


def test_an_image_switches_to_the_packaged_module() -> None:
    _start_proxy(["a.example.com"], None, image="toolforge-control:local")
    assert StubManager.seen == [
        {"hosts": ["a.example.com"], "broker": {}, "image": "toolforge-control:local", "packaged": True}
    ]


def test_a_failing_start_is_one_sandbox_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(StubManager):
        def start(self) -> StubManager:
            raise RuntimeError("egress proxy failed to start")

    monkeypatch.setattr(
        minilocker_runner._ml_path, "load", lambda name, settings=None: SimpleNamespace(EgressManager=Broken)
    )
    with pytest.raises(SandboxUnavailable):
        _start_proxy(["a.example.com"], None, image="x")


def test_the_setting_reaches_the_runners_pool() -> None:
    runner = MiniLockerRunner(settings=Settings(egress_proxy_image="toolforge-control:local"))
    runner.pool.acquire(["a.example.com"])
    assert (
        StubManager.seen[-1]["image"] == "toolforge-control:local"
        and StubManager.seen[-1]["packaged"] is True
    )


def test_without_the_setting_the_pool_uses_the_default_factory() -> None:
    runner = MiniLockerRunner(settings=Settings(egress_proxy_image=""))
    runner.pool.acquire(["a.example.com"])
    assert "image" not in StubManager.seen[-1] and "packaged" not in StubManager.seen[-1]


def test_the_setting_defaults_to_empty_and_is_documented() -> None:
    from pathlib import Path

    assert Settings().egress_proxy_image == ""
    root = Path(__file__).resolve().parent.parent
    assert "TOOLFORGE_EGRESS_PROXY_IMAGE" in (root / ".env.example").read_text(encoding="utf-8")
    assert "TOOLFORGE_EGRESS_PROXY_IMAGE" in (root / "docs" / "clients.md").read_text(encoding="utf-8")
