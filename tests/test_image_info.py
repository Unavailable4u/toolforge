"""`image_id` (P14a): the id Docker reports for an image, remembered when read and never invented."""

from __future__ import annotations

from toolforge.sandbox import image_info

ID = "sha256:" + "cd" * 32


def setup_function() -> None:
    image_info.forget()


def test_a_read_id_is_returned_and_kept() -> None:
    calls: list[str] = []

    def lookup(image: str) -> str | None:
        calls.append(image)
        return ID

    assert image_info.image_id("img:1", lookup=lookup) == ID
    assert image_info.image_id("img:1", lookup=lambda image: pytest_fail()) == ID
    assert calls == ["img:1"]


def pytest_fail() -> str:
    raise AssertionError("a known id must not be looked up again")


def test_a_failure_is_none_and_is_retried_only_after_a_minute() -> None:
    now = [100.0]
    answers = iter([None, ID])
    calls: list[str] = []

    def lookup(image: str) -> str | None:
        calls.append(image)
        return next(answers)

    clock = lambda: now[0]  # noqa: E731
    assert image_info.image_id("img:2", lookup=lookup, clock=clock) is None
    now[0] += 30
    assert image_info.image_id("img:2", lookup=lookup, clock=clock) is None and len(calls) == 1
    now[0] += 31
    assert image_info.image_id("img:2", lookup=lookup, clock=clock) == ID and len(calls) == 2


def test_a_lookup_that_raises_is_none() -> None:
    def boom(image: str) -> str | None:
        raise RuntimeError("daemon went away")

    assert image_info.image_id("img:3", lookup=boom) is None


def test_the_real_lookup_without_a_daemon_is_none(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    assert image_info.image_id("toolforge-runtime:none") is None
