"""`forge_runtime_info` against a real Docker daemon: the library list is read from the runtime image, with no network.

Skipped when no daemon answers, or when the runtime image is not already present locally (the probe never pulls an image).
"""

from __future__ import annotations

import pytest

from toolforge.config import Settings
from toolforge.server import runtime_info

pytestmark = pytest.mark.docker


def test_the_probe_reads_python_and_libraries_from_the_image() -> None:
    import docker

    image = Settings().runtime_image
    client = docker.from_env(timeout=5)
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        pytest.skip(f"the image {image} is not present locally (the probe never pulls)")
    finally:
        client.close()
    runtime_info.forget()
    facts = runtime_info.read_image(image)
    runtime_info.forget()
    assert facts["libraries_status"].startswith("measured")
    assert facts["python"] and facts["python"].count(".") == 2
    assert isinstance(facts["libraries"], list) and facts["libraries"]  # even a slim image has pip
    assert all({"name", "version"} <= set(item) for item in facts["libraries"])
