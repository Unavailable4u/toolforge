from __future__ import annotations

import pytest

from toolforge import __version__
from toolforge.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"toolforge {__version__}"


def test_config_masks_secrets(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("TOOLFORGE_LLM_API_KEY", "do-not-print-me")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "do-not-print-me" not in out and '"llm_api_key": "***"' in out


def test_doctor_stub(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    assert "no checks yet" in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: toolforge" in capsys.readouterr().out


def test_bad_setting_gives_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOOLFORGE_SERVER_PORT", "0")
    assert main(["config"]) == 1
    err = capsys.readouterr().err
    assert "error [config_error]" in err and "hint:" in err and "Traceback" not in err
