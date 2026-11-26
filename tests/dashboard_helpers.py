"""Shared set-up for the dashboard tests: a registry with versions in known states, and a dashboard on top of it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from verify_helpers import MemoryLedgers, scripted_gates, valid_package

from toolforge.config import Settings
from toolforge.dashboard.app import Dashboard, build_dashboard
from toolforge.registry import Registry
from toolforge.services.forge import Forge

HOST = "127.0.0.1"
PORT = 8787
BASE = f"http://{HOST}:{PORT}"
NETWORK = {"network": ["pypi.org"], "secrets": [], "inputs": False, "browser": False}
TOKEN = "t0ken-for-tests-only"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(home=tmp_path / "home", **{"dashboard_host": HOST, "dashboard_port": PORT, **overrides})


def submit(settings: Settings, registry: Registry, **kwargs: Any) -> Any:
    """Submit a package through the real pipeline with the cheap scripted gates; returns the verification report."""
    forge = Forge(settings, registry, None, gates=scripted_gates())
    forge._pipeline._approval._brokers = lambda: {}
    report = forge.submit_package(valid_package(**kwargs), "agent")
    assert report.registered, report.text()
    return report


def make_dashboard(
    tmp_path: Path,
    registry: Registry,
    ledgers: MemoryLedgers | None = None,
    *,
    runs_dir: Path | None = None,
    session_close_wait_s: float = 8.0,
    **overrides: Any,
) -> tuple[Dashboard, TestClient, MemoryLedgers]:
    ledgers = ledgers if ledgers is not None else MemoryLedgers()
    settings = make_settings(tmp_path, **overrides)
    dash = build_dashboard(
        settings,
        registry=registry,
        runs_dir=runs_dir or tmp_path / "runs",
        ledger_factory=ledgers,
        event_interval_s=0.05,
    )
    client = TestClient(dash.guarded, base_url=BASE, raise_server_exceptions=False)
    return dash, client, ledgers


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
