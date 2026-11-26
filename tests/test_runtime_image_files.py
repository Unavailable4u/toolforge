"""The runtime image's source (P14a): every library is an exact pin, the Dockerfile installs nothing else, and the documentation
lists exactly what the requirements file pins."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FOLDER = REPO / "images" / "runtime"
GUIDE_LIBRARIES = {
    "duckdb", "pyyaml", "python-dateutil", "beautifulsoup4", "lxml", "pillow", "numpy", "gymnasium", "chess", "jsonschema", "tabulate",
}  # fmt: skip


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins() -> dict[str, str]:
    found: dict[str, str] = {}
    for line in (FOLDER / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.]*)", line)
        assert match, f"not an exact pin: {line!r}"
        name = normalise(match.group(1))
        assert name not in found, f"{name} is pinned twice"
        found[name] = match.group(2)
    return found


def test_every_requirement_is_an_exact_pin_and_the_guides_libraries_are_all_there() -> None:
    assert GUIDE_LIBRARIES <= set(pins())


def test_the_dockerfile_installs_only_the_pinned_list_from_wheels_without_resolving_anything() -> None:
    text = (FOLDER / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.12-slim$", text, re.MULTILINE)
    install = re.search(r"pip install([^\n\\]*(?:\\\n[^\n\\]*)*)", text)
    assert install is not None
    command = install.group(1)
    assert (
        "--no-deps" in command and "--only-binary=:all:" in command and "-r /tmp/requirements.txt" in command
    )
    assert text.count("pip install") == 1 and "apt" not in text and "curl" not in text


def test_the_documentation_table_lists_exactly_the_pinned_libraries_and_versions() -> None:
    doc = (REPO / "docs" / "runtime-image.md").read_text()
    rows = dict(re.findall(r"^\| `([^`]+)` \| ([0-9][A-Za-z0-9.]*) \|", doc, re.MULTILINE))
    assert {normalise(name): version for name, version in rows.items()} == pins()
