"""Check example values against author-supplied JSON Schemas without letting a hostile schema hang or exhaust the host.

A schema is untrusted input. A catastrophic regular expression in `pattern`, or a chain of `$ref`s that fans out, can keep
`jsonschema` busy for hours. So `check_instances` runs the check in a child interpreter with a wall-clock timeout and a
memory cap, and kills it when the time is up. `tests/test_package_isolation.py` proves a regex bomb and a reference bomb are
both stopped. Anything that validates caller data against an author's schema on the host (P05, P06) should come through here.

This file is also the child's program (`python -I schemacheck.py`), so at module level it imports only the standard library
and `jsonschema`, never `toolforge`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator

DEFAULT_TIMEOUT_S = 5.0
_MEMORY_LIMIT_BYTES = 2 * 1024**3


class SchemaCheckError(Exception):
    """The check could not finish: it timed out, ran out of memory, or the child failed."""


def _printable(text: str, limit: int = 200) -> str:
    return "".join(char if char.isprintable() else "?" for char in text)[:limit]


def _describe(error: Any) -> str:
    where = "/".join(str(part) for part in error.absolute_path)
    text = _printable(error.message)
    return f"{text} (at /{where})" if where else text


def evaluate(request: Sequence[Mapping[str, Any]]) -> list[list[str | None]]:
    """For each `{"schema", "instances"}`: `None` for a valid instance, else a one-line reason. Runs in this process."""
    reply: list[list[str | None]] = []
    for check in request:
        results: list[str | None] = []
        try:
            validator = Draft202012Validator(check["schema"])
        except Exception as exc:
            reply.append([f"the schema could not be used ({type(exc).__name__})"] * len(check["instances"]))
            continue
        for instance in check["instances"]:
            try:
                error = next(iter(validator.iter_errors(instance)), None)
            except RecursionError:
                results.append("the schema refers to itself without end")
                continue
            except Exception as exc:
                results.append(f"could not be evaluated ({type(exc).__name__})")
                continue
            results.append(None if error is None else _describe(error))
        reply.append(results)
    return reply


def check_instances(
    checks: Sequence[tuple[Mapping[str, Any], Sequence[Any]]],
    *,
    isolated: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[list[str | None]]:
    """Check each `(schema, instances)` pair. Returns, per pair, one entry per instance: None when valid, else why not.

    Raises `SchemaCheckError` when the check cannot finish in `timeout_s` seconds. `isolated=False` runs in this process
    and is for tests of other code only: it has no timeout.
    """
    request = [{"schema": dict(schema), "instances": list(instances)} for schema, instances in checks]
    if not request:
        return []
    if not isolated:
        return evaluate(request)
    # The child is our own program, but it still gets no TOOLFORGE_* variables, so no key can reach it.
    env = {name: value for name, value in os.environ.items() if not name.startswith("TOOLFORGE_")}
    try:
        done = subprocess.run(
            [sys.executable, "-I", os.path.abspath(__file__)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SchemaCheckError(f"took longer than {timeout_s:g} seconds") from None
    except OSError as exc:
        raise SchemaCheckError(
            f"the checker could not start ({exc.strerror or type(exc).__name__})"
        ) from None
    if done.returncode != 0:
        raise SchemaCheckError("the checker stopped early (out of memory, or it crashed)")
    try:
        reply = json.loads(done.stdout)
        shaped = (
            isinstance(reply, list)
            and len(reply) == len(request)
            and all(
                isinstance(row, list) and len(row) == len(check["instances"])
                for row, check in zip(reply, request, strict=True)
            )
        )
    except ValueError:
        shaped = False
    if not shaped:
        raise SchemaCheckError("the checker returned something unreadable")
    return reply


def _main() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
    except (ImportError, ValueError, OSError):
        pass  # no resource module (Windows) or the limit cannot be set: the timeout still applies
    json.dump(evaluate(json.load(sys.stdin)), sys.stdout)


if __name__ == "__main__":
    _main()
