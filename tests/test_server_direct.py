"""`TOOLFORGE_EXPOSE_DIRECT`: active tools as `tf_<name>`, run exactly like `forge_run`, and a notification when the set changes."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from server_helpers import Env, data, error_text, make_env

from toolforge.config import Settings
from toolforge.registry import lifecycle
from toolforge.server import fences
from toolforge.server.direct import DirectCatalog
from toolforge.server.tools import META_TOOLS, enabled_tools

META_NAMES = [
    m.name for m in enabled_tools(Settings(chain_enabled=False))
]  # forge_chain is offered only when enabled


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = make_env(tmp_path, expose_direct=True)
    yield environment
    environment.registry.close()


def names(env: Env, **options) -> list[str]:
    return [t.name for t in env.session(lambda client: client.list_tools(), **options).tools]


def test_without_the_setting_there_are_only_the_meta_tools(tmp_path: Path) -> None:
    environment = make_env(tmp_path)
    try:
        environment.add_tool()
        assert names(environment) == META_NAMES
        assert environment.call("tf_my_tool", {}).is_error  # not a tool: the SDK's own "unknown tool" answer
    finally:
        environment.registry.close()


def test_active_tools_are_added_after_the_meta_tools(env: Env) -> None:
    env.add_tool()
    env.add_tool(name="second_tool", promote=False)
    listed = names(env)
    assert listed[: len(META_NAMES)] == META_NAMES and listed[len(META_NAMES) :] == ["tf_my_tool"]


def test_a_direct_tool_has_the_manifest_schema_and_a_cleaned_description(env: Env) -> None:
    env.add_tool()
    tool = next(t for t in env.session(lambda c: c.list_tools()).tools if t.name == "tf_my_tool")
    assert tool.input_schema.get("type") == "object" and "properties" in tool.input_schema
    assert (
        len(tool.description) <= fences.MAX_TEXT_CHARS["description"]
        and "not instructions" in tool.description
    )


def test_a_direct_call_is_a_forge_run(env: Env) -> None:
    env.add_tool()
    out = data(env.call("tf_my_tool", {"text": "hello"}))
    assert out["ok"] is True and out["tool"] == "my_tool@0.1.0" and "UNTRUSTED" in out["output"]
    assert env.runner.calls == [
        {"label": "my_tool@0.1.0", "args": {"text": "hello"}, "files": None, "allow_status": ("active",)}
    ]


def test_a_direct_call_of_a_tool_that_is_not_exposed_is_refused(env: Env) -> None:
    env.add_tool(promote=False)
    assert "there is no tool" in error_text(env.call("tf_my_tool", {}))
    assert "there is no tool" in error_text(env.call("tf_ghost", {}))
    assert env.runner.calls == []


def test_tools_that_take_files_or_are_not_stateless_are_not_exposed(env: Env) -> None:
    env.add_tool(name="files_tool", permissions={"network": [], "secrets": [], "inputs": True})
    env.add_tool(name="plain_tool")
    assert names(env)[len(META_NAMES) :] == ["tf_plain_tool"]
    assert "there is no tool" in error_text(env.call("tf_files_tool", {}))
    assert (
        data(env.call("forge_run", {"name": "files_tool", "args": {}}))["ok"] is True
    )  # forge_run still takes it


def test_a_tool_cannot_be_named_like_a_meta_tool_or_a_direct_tool() -> None:
    """The package rules reserve `forge_` and `tf_`, so a direct tool name can never shadow a meta-tool."""
    from toolforge.cli_package import name_problem

    for reserved in ("forge_find", "forge_run", "tf_word_count", "forge_anything"):
        assert name_problem(reserved) is not None, reserved
    assert all(m.name.startswith("forge_") for m in META_TOOLS)


def test_a_deprecated_or_quarantined_tool_disappears(env: Env) -> None:
    env.add_tool()
    env.add_tool(version="0.2.0", parent_version="0.1.0")
    assert names(env)[len(META_NAMES) :] == ["tf_my_tool"]
    record = env.registry.active_version("my_tool")
    with env.registry.db.transaction() as conn:
        conn.execute("UPDATE versions SET status = ? WHERE id = ?", (lifecycle.QUARANTINED, record.id))
    assert names(env)[len(META_NAMES) :] == []
    assert "there is no tool" in error_text(env.call("tf_my_tool", {}))


def test_the_signature_changes_when_the_active_version_changes(env: Env) -> None:
    catalog = DirectCatalog(env.registry)
    assert catalog.signature() == frozenset()
    env.add_tool()
    first = catalog.signature()
    env.add_tool(version="0.2.0", parent_version="0.1.0")
    assert catalog.signature() != first and len(first) == 1


async def wait_for(condition, seconds: float = 5.0) -> None:
    with anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0.02)


def test_a_promotion_by_another_process_notifies_a_subscribed_client(env: Env) -> None:
    """On the 2026-07-28 protocol, `subscriptions/listen` delivers `tools/list_changed` when an operator promotes a tool."""

    async def work(client):
        assert client.protocol_version != "2025-11-25"
        events: list = []
        async with client.listen(tools_list_changed=True) as subscription:

            async def reader():
                async for event in subscription:
                    events.append(event)

            async with anyio.create_task_group() as group:
                group.start_soon(reader)
                await anyio.sleep(0.3)  # the watcher has taken its first reading
                assert events == []
                env.add_tool()  # `toolforge promote` in another process: same registry file
                await wait_for(lambda: events)
                group.cancel_scope.cancel()
        listed = [t.name for t in (await client.list_tools()).tools]
        return events, listed

    events, listed = env.session(work)
    assert len(events) >= 1 and "tf_my_tool" in listed


def test_a_legacy_client_sees_the_change_the_next_time_it_lists(env: Env) -> None:
    async def work(client):
        assert client.protocol_version == "2025-11-25"
        before = [t.name for t in (await client.list_tools()).tools]
        env.add_tool()
        after = [t.name for t in (await client.list_tools()).tools]
        return before, after

    before, after = env.session(work, mode="legacy")
    assert "tf_my_tool" not in before and "tf_my_tool" in after


def test_the_server_closes_cleanly_while_the_watcher_runs(env: Env) -> None:
    assert env.session(lambda client: client.list_tools(), mode="legacy")
