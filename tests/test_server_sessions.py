"""Sessions over MCP (P13): `forge_run` with `session_id`, `forge_session_list`, `forge_session_rewind` and `forge_session_close`.

A real `ToolRunner` and session manager with the real harness (not isolated: trusted test code), behind the real MCP server.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from server_helpers import Env, StandInRunner, data, error_text
from sessions_helpers import SessionRig, counter_package, posix_only
from verify_helpers import scripted_gates

from toolforge.registry import lifecycle
from toolforge.server.app import build_server
from toolforge.services.forge import Forge

pytestmark = posix_only


@pytest.fixture
def mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Env, SessionRig]]:
    rig = SessionRig(tmp_path, monkeypatch)
    forge = Forge(rig.settings, rig.registry, None, gates=scripted_gates(), queue_timeout_s=0.2)
    app = build_server(
        rig.settings,
        registry=rig.registry,
        sandbox=object(),
        ledger_factory=rig.ledgers,
        forge=forge,
        tool_runner=rig.runner,
        watch_interval_s=0.05,
    )
    rig.add()
    yield Env(rig.settings, rig.registry, app, rig.ledgers, StandInRunner()), rig
    rig.close()


def run(
    env: Env, args: dict[str, Any] | None = None, session_id: str | None = None, name: str = "counter"
) -> Any:
    arguments: dict[str, Any] = {"name": name, "args": args or {}}
    if session_id is not None:
        arguments["session_id"] = session_id
    return env.call("forge_run", arguments)


def n_of(out: dict[str, Any]) -> int:
    """The counter's `n` inside the fenced, untrusted `output` text."""
    found = re.search(r'"n":\s*(-?\d+)', out["output"])
    assert found, out["output"]
    return int(found.group(1))


def plain(text: str) -> str:
    """An error text without the per-call reference token."""
    return re.sub(r"\[?ref [0-9a-f]+\]?", "", text)


def test_the_first_call_opens_a_session_and_returns_its_id(mcp) -> None:
    env, _ = mcp
    out = data(run(env, {"by": 1}))
    assert out["ok"] and n_of(out) == 1 and out["step"] == 1 and out["session_id"].startswith("s-")
    assert out["session_expires_in_s"] > 0


def test_a_call_with_the_id_continues_the_same_state(mcp) -> None:
    env, _ = mcp
    sid = data(run(env, {"by": 1}))["session_id"]
    assert [n_of(data(run(env, {"by": 2}, sid))) for _ in range(2)] == [3, 5]
    assert n_of(data(run(env, {"by": 1}))) == 1  # without the id it is a new session


def test_list_shows_the_callers_own_session_and_never_the_secret(mcp) -> None:
    env, _ = mcp
    sid = data(run(env, {}))["session_id"]
    text = json.dumps(data(env.call("forge_session_list", {"session_id": sid})))
    assert sid.split(".")[0] in text and sid.split(".")[1] not in text
    everything = json.dumps(data(env.call("forge_session_list", {})))
    assert sid.split(".")[1] not in everything and sid.split(".")[0] in everything


def test_rewind_goes_back_to_an_earlier_step(mcp) -> None:
    env, _ = mcp
    sid = data(run(env, {"by": 1}))["session_id"]
    data(run(env, {"by": 1}, sid))
    data(run(env, {"by": 1}, sid))
    back = data(env.call("forge_session_rewind", {"session_id": sid, "step": 1}))
    assert "1" in json.dumps(back)
    assert n_of(data(run(env, {"by": 1}, sid))) == 2


def test_close_ends_the_session_once_and_a_later_call_says_so(mcp) -> None:
    env, _ = mcp
    sid = data(run(env, {}))["session_id"]
    closed = data(env.call("forge_session_close", {"session_id": sid}))
    assert "closed" in json.dumps(closed)
    assert "session" in error_text(run(env, {}, sid)).lower()
    again = env.call("forge_session_close", {"session_id": sid})
    assert not again.is_error or "session" in error_text(again).lower()  # closing twice is harmless


def test_an_unknown_id_and_a_wrong_secret_get_the_same_answer(mcp) -> None:
    env, _ = mcp
    sid = data(run(env, {}))["session_id"]
    public = sid.split(".")[0]
    wrong_secret = plain(error_text(run(env, {}, f"{public}.{'0' * 32}")))
    unknown = plain(error_text(run(env, {}, "s-deadbeef." + "0" * 32)))
    malformed = plain(error_text(run(env, {}, "nonsense")))
    assert wrong_secret == unknown == malformed and "bad_session" in wrong_secret
    for tool_args in (
        {"session_id": f"{public}.{'0' * 32}", "step": 0},
        {"session_id": "s-deadbeef." + "0" * 32, "step": 0},
    ):
        assert "bad_session" in error_text(env.call("forge_session_rewind", tool_args))
    assert "bad_session" in error_text(
        env.call("forge_session_close", {"session_id": f"{public}.{'0' * 32}"})
    )
    assert data(run(env, {}, sid))["ok"]  # the wrong guesses did not disturb the real session


def test_a_session_only_runs_the_tool_that_opened_it(mcp) -> None:
    env, rig = mcp
    other = rig.add(counter_package("other_counter", "0.1.0"))
    sid = data(run(env, {}))["session_id"]
    assert "session_other_tool" in error_text(run(env, {}, sid, name=other.name))


def test_the_session_survives_its_version_being_deprecated(mcp) -> None:
    env, rig = mcp
    sid = data(run(env, {"by": 2}))["session_id"]
    record = rig.registry.active_version("counter")
    rig.registry.transition(record.id, lifecycle.DEPRECATED, actor="operator")
    assert n_of(data(run(env, {"by": 1}, sid))) == 3
    assert "no_active_version" in error_text(run(env, {}))  # a new session cannot start on it


def test_a_session_id_for_a_tool_that_cannot_hold_one_is_not_a_way_to_run_it(mcp) -> None:
    env, _ = mcp
    assert "bad_session" in error_text(run(env, {}, "s-00000000." + "1" * 32, name="counter"))


def test_the_tool_list_names_the_session_tools(mcp) -> None:
    env, _ = mcp

    async def names(client):
        return {t.name for t in (await client.list_tools()).tools}

    found = env.session(names)
    assert {"forge_session_close", "forge_session_rewind", "forge_session_list"} <= found
