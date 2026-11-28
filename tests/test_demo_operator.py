"""The demo's operator: promotes verified, permission-free versions, refuses the hostile names, denies approvals, and says what it did."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from demo import hostile
from demo.operator_script import DemoOperator


class FakeRegistry:
    def __init__(self, versions: list[Any], approvals: list[Any] | None = None) -> None:
        self.versions, self.approvals, self.closed = versions, approvals or [], False

    def list_versions(self, *, status: str) -> list[Any]:
        return [v for v in self.versions if v.status == status]

    def list_approvals(self, state: str) -> list[Any]:
        return self.approvals

    def close(self) -> None:
        self.closed = True


def version(id_: int, name: str, status: str = "verified", network: bool = False) -> Any:
    return SimpleNamespace(
        id=id_, name=name, label=f"{name}@1.0.0", status=status, asks_network_or_secrets=network
    )


class Calls(list):  # the calls the stand-ins received, with the fake registry they were given
    registry: FakeRegistry


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    from toolforge import cli_run
    from toolforge.registry import Registry
    from toolforge.services import operator_actions

    made = Calls()
    made.registry = FakeRegistry([])

    def promote(settings: Any, reg: Any, ref: str, reason: str, *, ledger_factory: Any = None) -> None:
        assert ledger_factory is cli_run.make_ledger, "a promotion must be written to the version's ledger"
        made.append(("promote", ref))

    def decide(
        settings: Any, reg: Any, ident: Any, verdict: str, reason: str, *, ledger_factory: Any = None
    ) -> None:
        assert ledger_factory is cli_run.make_ledger
        made.append((verdict, str(ident)))

    monkeypatch.setattr(Registry, "from_settings", classmethod(lambda cls, settings: made.registry))
    monkeypatch.setattr(operator_actions, "promote", promote)
    monkeypatch.setattr(operator_actions, "decide", decide)
    return made


def run(
    calls: Calls,
    versions: list[Any],
    approvals: list[Any] | None = None,
    refuse: tuple[str, ...] = hostile.NAMES,
) -> tuple[DemoOperator, list[dict[str, Any]]]:
    calls.registry.versions = versions
    calls.registry.approvals = approvals or []
    seen: list[dict[str, Any]] = []
    operator = DemoOperator(SimpleNamespace(), refuse, seen.append)
    operator.act()
    return operator, seen


def test_a_verified_version_without_network_is_promoted(calls: Calls) -> None:
    operator, seen = run(calls, [version(1, "sqlite_query"), version(2, "csv_totals", "verified_basic")])
    assert [c for c in calls if c[0] == "promote"] == [
        ("promote", "sqlite_query@1.0.0"),
        ("promote", "csv_totals@1.0.0"),
    ]
    assert [a["action"] for a in seen] == ["promote", "promote"] and operator.actions == seen


def test_a_refused_name_is_never_promoted_even_when_it_looks_verified(calls: Calls) -> None:
    _, seen = run(calls, [version(1, hostile.ATTEMPT_TWO, "verified_basic")])
    assert not [c for c in calls if c[0] == "promote"] and seen == [
        {"action": "refuse", "version": f"{hostile.ATTEMPT_TWO}@1.0.0"}
    ]


def test_a_version_that_asks_for_network_is_left_for_a_person(calls: Calls) -> None:
    _, seen = run(calls, [version(1, "fetcher", network=True)])
    assert not [c for c in calls if c[0] == "promote"] and seen[0]["action"] == "leave_pending"


def test_pending_approvals_are_denied_once(calls: Calls) -> None:
    operator, _ = run(calls, [], approvals=[SimpleNamespace(id="a1")])
    operator.act()
    assert [c for c in calls if c[0] == "deny"] == [("deny", "a1")]


def test_a_decision_is_made_once_per_version(calls: Calls) -> None:
    operator, _ = run(calls, [version(1, "sqlite_query")])
    operator.act()
    assert len([c for c in calls if c[0] == "promote"]) == 1


def test_the_registry_connection_is_always_closed(calls: Calls) -> None:
    run(calls, [version(1, "sqlite_query")])
    assert calls.registry.closed is True
