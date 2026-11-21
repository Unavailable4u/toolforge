from __future__ import annotations

from pathlib import Path

import pytest

from toolforge.config import Settings, load_settings
from toolforge.errors import ConfigError


def test_defaults() -> None:
    s = load_settings()
    assert s.home == Path("~/.toolforge").expanduser()
    assert s.runtime_image == "python:3.12-slim"
    assert s.build_allow == ["pypi.org", "files.pythonhosted.org"]
    assert (s.server_host, s.server_port, s.dashboard_host, s.dashboard_port) == (
        "127.0.0.1",
        8765,
        "127.0.0.1",
        8787,
    )
    assert s.max_package_kb == 256 and s.default_timeout_s == 20 and s.default_memory_mb == 256
    assert s.max_concurrent_verifications == 2 and s.approval_timeout_s == 900
    assert s.auto_promote is True and s.runtime_decoys is True and s.expose_direct is False
    assert s.llm_base_url is None and s.server_token is None


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TOOLFORGE_HOME", str(tmp_path))
    monkeypatch.setenv("TOOLFORGE_BUILD_ALLOW", "Pypi.org, example.org")
    monkeypatch.setenv("TOOLFORGE_AUTO_PROMOTE", "false")
    monkeypatch.setenv("TOOLFORGE_SERVER_PORT", "9000")
    monkeypatch.setenv("TOOLFORGE_LOG_LEVEL", "debug")
    s = load_settings()
    assert s.home == tmp_path
    assert s.build_allow == ["pypi.org", "example.org"]
    assert s.auto_promote is False and s.server_port == 9000 and s.log_level == "DEBUG"


def test_challenger_falls_back_to_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_LLM_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("TOOLFORGE_LLM_MODEL", "builder-model")
    s = load_settings()
    assert s.effective_challenger_base_url == "http://localhost:1/v1"
    assert s.effective_challenger_model == "builder-model"
    monkeypatch.setenv("TOOLFORGE_CHALLENGER_MODEL", "other-model")
    assert load_settings().effective_challenger_model == "other-model"


def test_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_LLM_API_KEY", "super-secret-value")
    monkeypatch.setenv("TOOLFORGE_SERVER_TOKEN", "another-secret")
    s = load_settings()
    shown = s.masked()
    assert shown["llm_api_key"] == "***" and shown["server_token"] == "***"
    assert shown["dashboard_token"] is None
    for text in (repr(s), str(shown)):
        assert "super-secret-value" not in text and "another-secret" not in text


def test_invalid_value_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_SERVER_PORT", "99999")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "TOOLFORGE_SERVER_PORT" in str(info.value)


def test_env_file_is_read(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TOOLFORGE_RUNTIME_IMAGE=custom:1\n", encoding="utf-8")
    assert Settings(_env_file=env).runtime_image == "custom:1"  # type: ignore[call-arg]


def test_server_allowed_hosts_and_origins_are_comma_separated_and_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_settings().server_allowed_hosts == [] and load_settings().server_allowed_origins == []
    monkeypatch.setenv("TOOLFORGE_SERVER_ALLOWED_HOSTS", " Tools.Internal , proxy.example.com:443,,")
    monkeypatch.setenv("TOOLFORGE_SERVER_ALLOWED_ORIGINS", "https://App.Example.com")
    s = load_settings()
    assert s.server_allowed_hosts == ["tools.internal", "proxy.example.com:443"]
    assert s.server_allowed_origins == ["https://app.example.com"]
