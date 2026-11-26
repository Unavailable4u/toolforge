"""The dashboard's network rules: loopback by default, a token for anything else, Host and Origin checks, no cookies, safe headers."""

from __future__ import annotations

from pathlib import Path

import pytest
from dashboard_helpers import BASE, HOST, PORT, TOKEN, auth, make_dashboard, make_settings, submit
from fastapi.testclient import TestClient

from toolforge.cli import main
from toolforge.dashboard.app import build_dashboard, describe_listener
from toolforge.errors import ConfigError
from toolforge.registry import Registry


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(tmp_path / "home") as reg:
        yield reg


def test_a_non_loopback_host_without_a_token_refuses_to_start(tmp_path, registry) -> None:
    settings = make_settings(tmp_path, dashboard_host="0.0.0.0")
    with pytest.raises(ConfigError) as caught:
        build_dashboard(settings, registry=registry)
    assert "TOOLFORGE_DASHBOARD_TOKEN" in str(caught.value) and "the dashboard" in str(caught.value)
    assert "TOOLFORGE_SERVER_TOKEN" not in str(caught.value)


def test_a_non_loopback_host_with_a_token_starts_and_the_token_is_never_printed(tmp_path, registry) -> None:
    settings = make_settings(tmp_path, dashboard_host="0.0.0.0", dashboard_token=TOKEN)
    build_dashboard(settings, registry=registry)
    line = describe_listener(settings)
    assert TOKEN not in line and "not loopback" in line and "bearer token required" in line
    assert "loopback only" in describe_listener(make_settings(tmp_path))


def test_the_command_refuses_a_non_loopback_host_without_a_token_before_listening(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TOOLFORGE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TOOLFORGE_DASHBOARD_TOKEN", raising=False)
    code = main(["dashboard", "--host", "0.0.0.0"])
    assert code != 0 and "TOOLFORGE_DASHBOARD_TOKEN" in capsys.readouterr().err


def test_the_api_needs_the_token_when_one_is_set_and_the_page_does_not(tmp_path, registry) -> None:
    _, client, _ = make_dashboard(tmp_path, registry, dashboard_token=TOKEN)
    assert client.get("/").status_code == 200
    assert (
        client.get("/static/app.js").status_code == 200 and client.get("/static/style.css").status_code == 200
    )
    for path in ("/api/meta", "/api/tools", "/api/approvals", "/api/sessions", "/api/events", "/api/attacks"):
        res = client.get(path)
        assert res.status_code == 401 and res.headers["www-authenticate"] == "Bearer", path
    assert client.get("/api/meta", headers=auth("wrong")).status_code == 401
    assert client.get("/api/meta", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.get("/api/meta", headers=auth()).status_code == 200
    assert client.get("/api/meta", headers=auth()).json()["token_required"] is True


def test_the_routes_check_the_token_themselves_as_well_as_the_guard(tmp_path, registry) -> None:
    dash, _, _ = make_dashboard(tmp_path, registry, dashboard_token=TOKEN)
    bare = TestClient(dash.app, raise_server_exceptions=False)  # the app without NetGuard in front of it
    for method, path in (("get", "/api/tools"), ("get", "/api/events"), ("post", "/api/approvals/1")):
        res = getattr(bare, method)(path, **({"json": {}} if method == "post" else {}))
        assert res.status_code == 401 and res.json()["error"]["code"] == "unauthorized", path
    assert bare.get("/api/meta", headers=auth()).status_code == 200


def test_a_write_without_the_token_changes_nothing(tmp_path, registry) -> None:
    submit(make_settings(tmp_path), registry)
    _, client, ledgers = make_dashboard(tmp_path, registry, dashboard_token=TOKEN)
    res = client.post("/api/versions/my_tool/0.1.0/promote", json={"reason": "x"})
    assert res.status_code == 401
    assert registry.find_version("my_tool", "0.1.0").status == "verified_basic" and ledgers.events == []


def test_api_paths_that_only_look_like_the_prefix_do_not_slip_past(tmp_path, registry) -> None:
    dash, client, _ = make_dashboard(tmp_path, registry, dashboard_token=TOKEN)
    # The guard decides by path; whatever the routes do with these, none of them hands out data without the token.
    for path in (
        "/api",
        "/api/",
        "//api/tools",
        "/api/../api/tools",
        "/API/tools",
        "/api%2Ftools",
        "/apiary",
    ):
        res = client.get(path)
        assert res.status_code in (401, 404, 307, 308), (path, res.status_code)
        assert "tools" not in res.text or res.status_code == 404, path
    assert (
        dash.guarded.evaluate([(b"host", f"{HOST}:{PORT}".encode())], None) == 401
    )  # an unknown path fails closed


def test_there_are_no_cookies(tmp_path, registry) -> None:
    _, client, _ = make_dashboard(tmp_path, registry, dashboard_token=TOKEN)
    for res in (client.get("/"), client.get("/api/meta", headers=auth()), client.get("/api/meta")):
        assert "set-cookie" not in res.headers
    assert "cookie" not in (client.get("/static/app.js").text.lower().replace("never in a cookie", ""))


def test_another_origin_cannot_use_the_api_even_without_a_token(tmp_path, registry) -> None:
    submit(make_settings(tmp_path), registry)
    _, client, ledgers = make_dashboard(tmp_path, registry)  # loopback, no token
    evil = {"Origin": "https://evil.example"}
    assert client.get("/api/tools", headers=evil).status_code == 403
    res = client.post("/api/versions/my_tool/0.1.0/promote", headers=evil, content='{"reason":"x"}')
    assert res.status_code == 403
    assert registry.find_version("my_tool", "0.1.0").status == "verified_basic" and ledgers.events == []
    assert client.get("/api/tools", headers={"Origin": BASE}).status_code == 200


def test_a_foreign_host_header_is_refused_whatever_the_path(tmp_path, registry) -> None:
    _, client, _ = make_dashboard(tmp_path, registry)
    rebinding = {"Host": f"attacker.example:{PORT}"}
    assert client.get("/", headers=rebinding).status_code == 403
    assert client.get("/api/tools", headers=rebinding).status_code == 403


def test_extra_origins_and_hosts_are_allowed_only_when_configured(tmp_path, registry) -> None:
    _, client, _ = make_dashboard(
        tmp_path,
        registry,
        dashboard_allowed_origins=["https://ops.example"],
        dashboard_allowed_hosts=["ops.example"],
    )
    assert (
        client.get("/api/meta", headers={"Origin": "https://ops.example", "Host": "ops.example"}).status_code
        == 200
    )
    assert client.get("/api/meta", headers={"Origin": "https://other.example"}).status_code == 403


def test_a_state_changing_request_must_be_json(tmp_path, registry) -> None:
    submit(make_settings(tmp_path), registry)
    _, client, ledgers = make_dashboard(tmp_path, registry)
    for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        res = client.post(
            "/api/versions/my_tool/0.1.0/promote", headers={"Content-Type": kind}, content='{"reason":"x"}'
        )
        assert res.status_code == 415 and res.json()["error"]["code"] == "json_required", kind
    assert client.post("/api/versions/my_tool/0.1.0/promote", content='{"reason":"x"}').status_code == 415
    assert registry.find_version("my_tool", "0.1.0").status == "verified_basic" and ledgers.events == []


def test_the_page_and_the_api_carry_safe_headers(tmp_path, registry) -> None:
    _, client, _ = make_dashboard(tmp_path, registry)
    page = client.get("/")
    policy = page.headers["content-security-policy"]
    assert (
        "script-src 'self'" in policy and "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]
    )
    assert (
        "frame-ancestors 'none'" in policy
        and "default-src 'none'" in policy
        and "connect-src 'self'" in policy
    )
    api = client.get("/api/meta")
    assert "sandbox" in api.headers["content-security-policy"]
    for res in (page, api):
        assert (
            res.headers["x-content-type-options"] == "nosniff" and res.headers["cache-control"] == "no-store"
        )
        assert res.headers["referrer-policy"] == "no-referrer"


def test_only_the_three_page_files_are_served_and_no_path_escapes(tmp_path, registry) -> None:
    _, client, _ = make_dashboard(tmp_path, registry)
    assert client.get("/static/index.html").status_code == 404
    for path in (
        "/static/../app.py",
        "/static/%2e%2e/app.py",
        "/static/app.py",
        "/static/..%2fapp.py",
        "/app.py",
        "/openapi.json",
        "/docs",
    ):
        assert client.get(path).status_code == 404, path


def test_the_dashboard_has_no_agent_route(tmp_path, registry) -> None:
    # An MCP client is told about forge_* tools only; none of the dashboard's writes is one, and the server never imports this package.
    from toolforge.server import tools as server_tools

    assert not any("dashboard" in name for name in dir(server_tools))
    source = Path(server_tools.__file__).parent
    for path in source.glob("*.py"):
        assert "toolforge.dashboard" not in path.read_text(), path.name


def test_the_listener_defaults_are_loopback_8787(tmp_path) -> None:
    settings = make_settings(tmp_path)
    assert settings.dashboard_host == HOST and settings.dashboard_port == PORT
    assert TestClient is not None
