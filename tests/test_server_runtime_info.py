"""`server/runtime_info.py` without Docker: what is cached, what is rejected, and that an unreadable image says "not measured"."""

from __future__ import annotations

import pytest

from toolforge.server import runtime_info


@pytest.fixture(autouse=True)
def _fresh():
    runtime_info.forget()
    yield
    runtime_info.forget()


def test_a_good_reading_is_returned_and_cached() -> None:
    calls: list[str] = []

    def probe(image: str):
        calls.append(image)
        return {"python": "3.12.4", "libraries": [["pip", "24.0"], ["wheel", "0.43"]]}

    first = runtime_info.read_image("img:1", probe)
    again = runtime_info.read_image("img:1", probe)
    assert first == again and calls == ["img:1"]
    assert first["python"] == "3.12.4" and first["libraries_status"] == "measured"
    assert first["libraries"] == [{"name": "pip", "version": "24.0"}, {"name": "wheel", "version": "0.43"}]


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"python": "3.12", "libraries": "x"},
        {"libraries": []},
        {"python": 3, "libraries": []},
        {"python": "3", "libraries": [["a"]]},
    ],
)
def test_an_unreadable_or_malformed_reading_is_not_measured_and_never_an_empty_list(answer) -> None:
    facts = runtime_info.read_image("img:2", lambda image: answer)
    assert facts["python"] is None and facts["libraries"] is None
    assert facts["libraries_status"].startswith("not measured")


def test_a_failed_reading_is_not_cached() -> None:
    answers = iter([None, {"python": "3.12.4", "libraries": []}])
    assert runtime_info.read_image("img:3", lambda image: next(answers))["libraries"] is None
    later = runtime_info.read_image("img:3", lambda image: next(answers))
    assert (
        later["libraries"] == [] and later["libraries_status"] == "measured"
    )  # an image with no libraries is a real, measured answer


def test_a_very_long_library_list_is_capped_and_says_so() -> None:
    many = [[f"lib{i}", "1"] for i in range(runtime_info.MAX_LIBRARIES + 50)]
    facts = runtime_info.read_image("img:4", lambda image: {"python": "3.12.4", "libraries": many})
    assert len(facts["libraries"]) == runtime_info.MAX_LIBRARIES and "first" in facts["libraries_status"]


def test_without_a_docker_daemon_the_real_probe_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    assert runtime_info.docker_probe("python:3.12-slim") is None
