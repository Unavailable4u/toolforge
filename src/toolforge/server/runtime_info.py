"""`forge_runtime_info`: what a tool's code will find when it runs. Read from the runtime image, never guessed.

The Python version and the installed libraries are properties of the image, so they are read from it: one throwaway container with no
network, a read-only root filesystem, no capabilities and a small memory limit runs a fixed script that lists the installed
distributions. The script and the image name are ToolForge's own; nothing from a caller reaches the container. `image_id` is the id Docker reports for
the image (the same value the ledgers record), or `null` when it could not be read.

When Docker is not available, the image is not present locally (the probe never pulls, so it never uses the network) or the probe
fails, `python` and `libraries` are `null` and `libraries_status` says "not measured" and why. An empty list always means "the image
has no libraries", never "not looked".

Proven by `tests/test_server_runtime_info.py` and `tests/test_server_tools.py` (a stand-in probe), and `tests/docker/test_server_runtime_probe.py` (the real one, with Docker).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 30
MAX_LIBRARIES = 400
_SCRIPT = (
    "import json, sys\n"
    "from importlib import metadata\n"
    "libs = sorted({(d.metadata['Name'] or '', d.version or '') for d in metadata.distributions() if d.metadata['Name']})\n"
    "print(json.dumps({'python': '%d.%d.%d' % sys.version_info[:3], 'libraries': [list(x) for x in libs]}))\n"
)

Probe = Callable[[str], "dict[str, Any] | None"]
_cache: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def docker_probe(image: str) -> dict[str, Any] | None:
    """`{"python": "3.12.4", "libraries": [["name", "version"], ...]}` read from `image`, or None when it cannot be read."""
    try:
        import docker
    except ImportError:
        return None
    client = None
    container = None
    try:
        client = docker.from_env(timeout=5)
        client.ping()
        found = client.images.get(image)  # never pull: that would be network access chosen by a request
        container = client.containers.run(
            image,
            ["python", "-I", "-c", _SCRIPT],
            detach=True,
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            mem_limit="128m",
            pids_limit=64,
            user="65534:65534",
        )
        container.wait(timeout=PROBE_TIMEOUT_S)
        out = container.logs(stdout=True, stderr=False).decode("utf-8", "replace").strip().splitlines()
        data = json.loads(out[-1]) if out else None
        if not isinstance(data, dict):
            return None
        identifier = str(getattr(found, "id", "") or "")
        if identifier.startswith("sha256:"):
            data["image_id"] = identifier
        return data
    except Exception:
        log.info("could not read the runtime image (Docker unavailable, image missing or the probe failed)")
        log.debug("probe failure detail", exc_info=True)
        return None
    finally:
        try:
            if container is not None:
                container.remove(force=True)
        except Exception:
            log.debug("could not remove the probe container", exc_info=True)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def read_image(image: str, probe: Probe | None = None) -> dict[str, Any]:
    """The facts about `image`: `python` and `libraries` (None when not measured), `libraries_status`. Cached for the process."""
    with _lock:
        cached = _cache.get(image)
    if cached is not None:
        return cached
    data = (probe or docker_probe)(image)
    libraries = data.get("libraries") if isinstance(data, dict) else None
    python = data.get("python") if isinstance(data, dict) else None
    if (
        not isinstance(libraries, list)
        or not isinstance(python, str)
        or not all(isinstance(x, list | tuple) and len(x) == 2 for x in libraries)
    ):
        return {
            "python": None,
            "libraries": None,
            "libraries_status": "not measured: the runtime image could not be read (Docker is not available, or the image is not present locally)",
            "image_id": None,
        }
    shown = [
        {"name": str(name)[:80], "version": str(version)[:40]} for name, version in libraries[:MAX_LIBRARIES]
    ]
    image_id = data.get("image_id") if isinstance(data, dict) else None
    facts: dict[str, Any] = {
        "python": python[:20],
        "image_id": image_id[:80] if isinstance(image_id, str) and image_id.startswith("sha256:") else None,
        "libraries": shown,
        "libraries_status": "measured"
        if len(libraries) <= MAX_LIBRARIES
        else f"measured; first {MAX_LIBRARIES} of {len(libraries)}",
    }
    with _lock:
        _cache[image] = facts
    return facts


def forget() -> None:
    """Drop what was read (tests)."""
    with _lock:
        _cache.clear()


__all__ = ["Probe", "docker_probe", "forget", "read_image"]
