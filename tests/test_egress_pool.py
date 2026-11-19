"""EgressPool: one proxy per process, shared only when its allowlist already covers the request, never restarted under a user."""

from __future__ import annotations

import threading

import pytest

from toolforge.errors import SandboxUnavailable
from toolforge.sandbox.minilocker_runner import EgressPool


class StubManager:
    def __init__(self, hosts: list[str]) -> None:
        self.hosts = hosts
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


class Factory:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_next = False

    def __call__(self, hosts: list[str]) -> StubManager:
        self.calls.append(list(hosts))
        if self.fail_next:
            self.fail_next = False
            raise SandboxUnavailable("proxy would not start")
        return StubManager(list(hosts))


def test_first_acquire_starts_the_proxy_with_sorted_lowercase_hosts() -> None:
    factory = Factory()
    pool = EgressPool(factory)
    manager = pool.acquire(["B.example.com", "a.example.com"])
    assert factory.calls == [["a.example.com", "b.example.com"]] and manager.hosts == factory.calls[0]
    assert pool.users == 1 and pool.allowed == {"a.example.com", "b.example.com"}


def test_a_covered_request_shares_the_running_proxy() -> None:
    factory = Factory()
    pool = EgressPool(factory)
    first = pool.acquire(["a.example.com", "b.example.com"])
    second = pool.acquire(["A.example.com"])
    assert second is first and len(factory.calls) == 1 and pool.users == 2


def test_a_wider_request_is_refused_while_anyone_is_using_the_proxy() -> None:
    factory = Factory()
    pool = EgressPool(factory)
    pool.acquire(["a.example.com"])
    with pytest.raises(SandboxUnavailable, match="egress is busy") as caught:
        pool.acquire(["a.example.com", "c.example.com"])
    assert "retry" in (caught.value.hint or "")
    assert (
        len(factory.calls) == 1 and pool.users == 1 and pool.allowed == {"a.example.com"}
    )  # nothing changed


def test_when_idle_a_wider_request_restarts_with_the_union() -> None:
    factory = Factory()
    pool = EgressPool(factory)
    first = pool.acquire(["a.example.com"])
    pool.release()
    second = pool.acquire(["c.example.com"])
    assert factory.calls == [["a.example.com"], ["a.example.com", "c.example.com"]]
    assert second is not first and pool.users == 1 and pool.allowed == {"a.example.com", "c.example.com"}


def test_the_allowlist_only_grows_until_shutdown() -> None:
    pool = EgressPool(Factory())
    pool.acquire(["a.example.com"])
    pool.release()
    pool.acquire(["b.example.com"])
    pool.release()
    assert pool.allowed == {
        "a.example.com",
        "b.example.com",
    }  # the documented limit: a later tool may reach an earlier host
    assert pool.shutdown() is True and pool.allowed == frozenset()


def test_a_failed_start_leaves_nothing_behind_and_counts_no_user() -> None:
    factory = Factory()
    pool = EgressPool(factory)
    pool.acquire(["a.example.com"])
    pool.release()
    factory.fail_next = True
    with pytest.raises(SandboxUnavailable, match="would not start"):
        pool.acquire(["b.example.com"])
    # Starting a proxy removes the previous one first, so the old manager is gone and must not be handed out again.
    assert pool.users == 0 and pool.allowed == frozenset()
    pool.acquire(["a.example.com"])
    assert factory.calls[-1] == ["a.example.com"]


def test_release_never_goes_below_zero() -> None:
    pool = EgressPool(Factory())
    pool.release()
    pool.release()
    assert pool.users == 0
    pool.acquire(["a.example.com"])
    assert pool.users == 1


def test_shutdown_refuses_while_in_use_and_stops_the_proxy_when_idle() -> None:
    pool = EgressPool(Factory())
    manager = pool.acquire(["a.example.com"])
    assert pool.shutdown() is False and manager.stopped == 0 and pool.users == 1
    pool.release()
    assert pool.shutdown() is True and manager.stopped == 1
    assert pool.shutdown() is True and manager.stopped == 1  # nothing is running now


def test_shutdown_of_a_pool_that_never_started_is_fine() -> None:
    assert EgressPool(Factory()).shutdown() is True


def test_concurrent_acquires_start_exactly_one_proxy() -> None:
    factory = Factory()
    pool = EgressPool(factory)
    managers: list[StubManager] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(20)

    def work() -> None:
        try:
            barrier.wait()
            managers.append(pool.acquire(["a.example.com"]))
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(factory.calls) == 1 and pool.users == 20
    assert len({id(m) for m in managers}) == 1


def test_concurrent_conflicting_requests_never_restart_a_proxy_under_a_user() -> None:
    factory = Factory()
    pool = EgressPool(factory)
    pool.acquire(["a.example.com"])
    results: list[str] = []

    def work(host: str) -> None:
        try:
            pool.acquire([host])
            results.append("ok")
        except SandboxUnavailable:
            results.append("busy")

    threads = [threading.Thread(target=work, args=(f"h{i}.example.com",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["busy"] * 10 and len(factory.calls) == 1 and pool.users == 1
