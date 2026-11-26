"""The id Docker reports for a local image, so a ledger can say which image a call ran in (P14a).

`image_id("toolforge-runtime:py312")` returns `sha256:...` read from the local Docker daemon, or None when it cannot be read: no
`docker` package, no daemon, or no such image. It never pulls an image and never starts a container. A successful answer is kept for
the life of the process (an image id does not change for a tag until the tag is rebuilt, and a server that outlives a rebuild is told so
by `toolforge doctor`); a failure is remembered for a minute only, so a daemon that starts later is found without a restart.

None is recorded as such: a ledger never invents a digest. Proven by `tests/test_image_info.py`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

RETRY_AFTER_S = 60.0
_lock = threading.Lock()
_found: dict[str, str] = {}
_missed: dict[str, float] = {}


def _from_docker(image: str) -> str | None:
    try:
        import docker
    except ImportError:
        return None
    client: Any = None
    try:
        client = docker.from_env(timeout=5)
        found = client.images.get(image)
        identifier = str(getattr(found, "id", "") or "")
        return identifier if identifier.startswith("sha256:") else None
    except Exception:
        log.debug("could not read the id of %s", image, exc_info=True)
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def image_id(
    image: str,
    *,
    lookup: Callable[[str], str | None] = _from_docker,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """The image id of `image`, or None when it could not be read (never raises)."""
    with _lock:
        if image in _found:
            return _found[image]
        missed = _missed.get(image)
        if missed is not None and clock() - missed < RETRY_AFTER_S:
            return None
    try:
        found = lookup(image)
    except Exception:
        found = None
    with _lock:
        if found:
            _found[image] = found
            _missed.pop(image, None)
        else:
            _missed[image] = clock()
    return found


def forget() -> None:
    """Drop what was read (tests, and after a rebuild)."""
    with _lock:
        _found.clear()
        _missed.clear()


__all__ = ["forget", "image_id"]
