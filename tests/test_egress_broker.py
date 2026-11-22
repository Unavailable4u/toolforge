"""Brokered services in the sandbox layer: the spec, the egress pool, and MiniLocker's reading of the broker configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_runner_with_stand_in import Factory, FakeManager, spec

from toolforge.errors import SandboxUnavailable
from toolforge.sandbox import SandboxSpec
from toolforge.sandbox.minilocker_runner import (
    EgressPool,
    MiniLockerRunner,
    broker_service_hosts,
    configured_broker_services,
)

ENTRY = {"host": "api.github.com", "header": "Authorization", "prefix": "Bearer ", "token": "ghp_" + "x" * 30}


class BrokerManager(FakeManager):
    def __init__(self, hosts: list[str], broker: dict | None = None) -> None:
        super().__init__(hosts)
        self.broker = broker


def loader(names):  # type: ignore[no-untyped-def]
    return {n: dict(ENTRY) for n in names}


def test_the_spec_normalises_service_names() -> None:
    s = SandboxSpec(image="i", network="none", broker=("GitHub", " github ", "slack"))
    assert s.broker == ("github", "slack")
    assert SandboxSpec(image="i", network="none").broker == ()


@pytest.mark.parametrize("bad", ["github", ("1bad",), ("has space",), ("x" * 30,), (5,), ("",)])
def test_the_spec_rejects_bad_service_names(bad: Any) -> None:
    with pytest.raises(ValueError):
        SandboxSpec(image="i", network="none", broker=bad)


def test_the_pool_starts_the_proxy_with_only_the_requested_services() -> None:
    managers: list[BrokerManager] = []

    def factory(hosts, broker=None):  # type: ignore[no-untyped-def]
        managers.append(BrokerManager(hosts, broker))
        return managers[-1]

    asked: list[list[str]] = []

    def loader_spy(names):  # type: ignore[no-untyped-def]
        asked.append(sorted(names))
        return loader(names)

    pool = EgressPool(factory, broker_loader=loader_spy)
    manager = pool.acquire((), ["github"])
    assert manager.hosts == [] and list(manager.broker) == ["github"] and asked == [["github"]]
    assert pool.services == {"github"} and pool.users == 1


def test_a_covered_request_shares_the_proxy_and_a_new_service_waits_for_idle() -> None:
    made: list[BrokerManager] = []

    def factory(hosts, broker=None):  # type: ignore[no-untyped-def]
        made.append(BrokerManager(hosts, broker))
        return made[-1]

    pool = EgressPool(factory, broker_loader=loader)
    first = pool.acquire(("a.example",), ["github"])
    assert pool.acquire(("a.example",), ["github"]) is first and len(made) == 1
    with pytest.raises(SandboxUnavailable) as caught:
        pool.acquire((), ["slack"])
    assert "busy" in str(caught.value)
    pool.release()
    pool.release()
    second = pool.acquire((), ["slack"])
    assert second is not first and pool.services == {"github", "slack"} and pool.allowed == {"a.example"}
    assert sorted(second.broker) == ["github", "slack"]


def test_a_host_only_pool_still_calls_the_factory_with_hosts_only() -> None:
    pool = EgressPool(lambda hosts: FakeManager(hosts))
    pool.acquire(("a.example",))
    assert pool.services == frozenset()


def test_a_service_nobody_configured_stops_the_start_and_clears_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toolforge.sandbox import minilocker_runner

    monkeypatch.setattr(minilocker_runner, "configured_broker_services", lambda *a, **k: {})
    pool = EgressPool(lambda hosts, broker=None: BrokerManager(hosts, broker))
    with pytest.raises(SandboxUnavailable) as caught:
        pool.acquire((), ["github"])
    assert "github" in str(caught.value) and "MINILOCKER_BROKER" in caught.value.hint
    assert pool.users == 0 and pool.services == frozenset()


def test_a_sandbox_that_only_needs_a_service_is_attached_to_the_proxy(tmp_path: Path) -> None:
    pool = EgressPool(lambda hosts, broker=None: BrokerManager(hosts, broker), broker_loader=loader)
    runner = MiniLockerRunner(pool=pool, sandbox_factory=Factory())
    handle = runner.open(spec(tmp_path, network="none", broker=("github",)))
    assert pool.users == 1 and pool.services == {"github"} and pool.allowed == frozenset()
    handle.close()
    assert pool.users == 0


def test_a_sandbox_with_neither_never_touches_the_proxy(tmp_path: Path) -> None:
    pool = EgressPool(lambda *a, **k: pytest.fail("no proxy may start"))
    MiniLockerRunner(pool=pool, sandbox_factory=Factory()).open(spec(tmp_path, network="none"))
    assert pool.users == 0


def test_broker_configuration_is_read_by_minilockers_own_parser() -> None:
    environ = {
        "MINILOCKER_BROKER": "github=api.github.com,bad name=x",
        "MINILOCKER_BROKER_TOKEN_GITHUB": "ghp_" + "y" * 30,
    }
    assert broker_service_hosts(environ=environ) == {"github": "api.github.com"}
    assert configured_broker_services(environ=environ)["github"]["token"].startswith(
        "ghp_"
    )  # the proxy needs it


def test_a_service_without_a_token_does_not_exist() -> None:
    assert broker_service_hosts(environ={"MINILOCKER_BROKER": "github=api.github.com"}) == {}
    assert broker_service_hosts(environ={}) == {}
