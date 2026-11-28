"""docker-compose.yml: loopback only, no raw socket outside the proxy, tokens required, the proxy allows only what MiniLocker calls.

The text checks need no Docker. `docker compose config` runs only when the CLI and its compose plugin are installed (it does not need a daemon).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
TEXT = COMPOSE.read_text(encoding="utf-8")
ALLOWED = {"CONTAINERS", "NETWORKS", "IMAGES", "EXEC", "POST", "VERSION", "INFO", "PING"}


def proxy_environment() -> dict[str, str]:
    block = re.search(r"^  docker-proxy:\n(.*?)^  \S", TEXT + "\n  x", re.S | re.M)
    assert block, "no docker-proxy service"
    env = re.search(r"environment:\n((?:      .*\n)+)", block.group(1))
    assert env
    out = {}
    for line in env.group(1).splitlines():
        if ":" in line and not line.strip().startswith("#"):
            key, _, value = line.strip().partition(":")
            out[key] = value.split("#")[0].strip().strip('"')
    return out


def test_the_proxy_allows_exactly_the_families_minilocker_calls() -> None:
    env = proxy_environment()
    assert {k for k, v in env.items() if v == "1"} == ALLOWED
    assert all(v == "0" for k, v in env.items() if k not in ALLOWED)
    for denied in ("BUILD", "VOLUMES", "COMMIT", "SYSTEM", "SWARM", "SECRETS"):
        assert env[denied] == "0", denied


def test_only_the_proxy_mounts_the_docker_socket_and_it_is_read_only() -> None:
    code = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    mount = "/var/run/docker.sock:/var/run/docker.sock:ro"
    assert code.count(mount) == 1 and "docker.sock" not in code.replace(mount, "")
    proxy = TEXT.split("  docker-proxy:")[1].split("\n  runtime-image:")[0]
    assert "docker.sock" in proxy


def test_every_published_port_is_on_loopback() -> None:
    ports = re.findall(r'^\s+- "([^"]+:\d+|[^"]+:\$\{[^}]+\})"$', TEXT, re.M)
    published = [p for p in ports if re.match(r"^[^:]+:", p) and ("${TOOLFORGE_" in p)]
    assert len(published) == 2 and all(p.startswith("127.0.0.1:") for p in published)
    assert "0.0.0.0:" not in TEXT.replace("TOOLFORGE_SERVER_HOST: 0.0.0.0", "").replace(
        "TOOLFORGE_DASHBOARD_HOST: 0.0.0.0", ""
    )


def test_both_tokens_are_required_with_a_message() -> None:
    for name in ("TOOLFORGE_SERVER_TOKEN", "TOOLFORGE_DASHBOARD_TOKEN"):
        assert re.search(rf"{name}: \$\{{{name}:\?[^}}]+\}}", TEXT), name


def test_the_control_plane_is_locked_down() -> None:
    assert "privileged" not in TEXT and "network_mode: host" not in TEXT and "pid: host" not in TEXT
    anchor = TEXT.split("x-control: &control")[1].split("services:")[0]
    for needle in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "pids_limit", "mem_limit"):
        assert needle in anchor, needle


def test_the_server_and_the_dashboard_use_the_packaged_egress_image_and_the_proxy() -> None:
    assert "DOCKER_HOST: tcp://docker-proxy:2375" in TEXT
    assert "TOOLFORGE_EGRESS_PROXY_IMAGE: toolforge-control:local" in TEXT
    assert 'command: ["toolforge", "serve", "--transport", "http"]' in TEXT
    assert 'command: ["toolforge", "dashboard", "--runs-dir", "/runs"]' in TEXT


def test_the_runtime_image_is_built_by_compose_not_through_the_proxy() -> None:
    assert "context: ./images/runtime" in TEXT and "image: toolforge-runtime:py312" in TEXT
    assert (ROOT / "images" / "runtime" / "Dockerfile").is_file() and (
        ROOT / "deploy" / "Dockerfile"
    ).is_file()
    assert "service_completed_successfully" in TEXT


def test_the_image_find_minilocker_and_runs_unprivileged() -> None:
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "TOOLFORGE_MINILOCKER_PATH=/app/third_party/minilocker/backend" in text
    assert "USER 10001:10001" in text and "chmod -R a+rX" in text
    assert "boto3" not in text and "moto" not in text


def test_the_build_context_keeps_secrets_and_results_out() -> None:
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for needle in (".env", ".git", "runs", ".venv"):
        assert needle in ignore, needle
    assert (
        ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        or (ROOT / ".env.example").is_file()
    )


def test_the_makefile_has_the_targets_the_docs_name() -> None:
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("demo-up", "demo-down", "demo-logs", "demo", "cover", "slides"):
        assert re.search(rf"^{target}:", make, re.M), target
    assert "docker compose up -d --build" in make


def compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


needs_compose = pytest.mark.skipif(
    not compose_available(), reason="the docker CLI with the compose plugin is not installed"
)


def config(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("TOOLFORGE_")}
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--project-directory",
            str(ROOT),
            "--env-file",
            os.devnull,
            "config",
        ],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@needs_compose
def test_compose_config_validates_with_both_tokens() -> None:
    done = config({"TOOLFORGE_SERVER_TOKEN": "a" * 40, "TOOLFORGE_DASHBOARD_TOKEN": "b" * 40})
    assert done.returncode == 0, done.stderr[-600:]
    assert "127.0.0.1" in done.stdout


@needs_compose
@pytest.mark.parametrize("missing", ["TOOLFORGE_SERVER_TOKEN", "TOOLFORGE_DASHBOARD_TOKEN"])
def test_compose_refuses_to_start_without_a_token(missing: str) -> None:
    env = {"TOOLFORGE_SERVER_TOKEN": "a" * 40, "TOOLFORGE_DASHBOARD_TOKEN": "b" * 40}
    del env[missing]
    done = config(env)
    assert done.returncode != 0 and missing in done.stderr
