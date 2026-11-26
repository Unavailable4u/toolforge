"""The runtime image built for real (P14a): `toolforge images build` makes it, and what is inside is exactly what the requirements pin.

Needs Docker and, once, the network (the base image and the wheels). Skipped when no daemon answers.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from toolforge import cli_images
from toolforge.config import RUNTIME_IMAGE
from toolforge.sandbox import image_info
from toolforge.server import runtime_info

pytestmark = pytest.mark.docker

REQUIREMENTS = Path(__file__).resolve().parent.parent.parent / "images" / "runtime" / "requirements.txt"
MODULES = "duckdb yaml dateutil bs4 lxml PIL numpy gymnasium chess jsonschema tabulate"


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@pytest.fixture(scope="module")
def built() -> str:
    code = cli_images.build_image()
    if code != 0:
        pytest.skip("the image could not be built (it needs the network once)")
    image_info.forget()
    runtime_info.forget()
    return RUNTIME_IMAGE


def test_the_built_image_has_an_id_and_python_3_12(built: str) -> None:
    assert (image_info.image_id(built) or "").startswith("sha256:")
    facts = runtime_info.read_image(built)
    assert facts["python"].startswith("3.12.") and facts["image_id"] == image_info.image_id(built)


def test_the_libraries_in_the_image_are_exactly_the_pinned_ones(built: str) -> None:
    pinned = {}
    for line in REQUIREMENTS.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.strip().split("==")
            pinned[normalise(name)] = version
    facts = runtime_info.read_image(built)
    installed = {normalise(item["name"]): item["version"] for item in facts["libraries"]}
    for name, version in pinned.items():
        assert installed.get(name) == version, (
            f"{name}: image has {installed.get(name)}, requirements pin {version}"
        )
    extra = set(installed) - set(pinned) - {"pip", "setuptools", "wheel"}
    assert not extra, f"the image has libraries that are not pinned: {sorted(extra)}"


def test_every_library_imports_with_no_network(built: str) -> None:
    script = "import " + ", ".join(MODULES.split()) + "; print('imports ok')"
    done = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", built, "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0 and "imports ok" in done.stdout, done.stderr
