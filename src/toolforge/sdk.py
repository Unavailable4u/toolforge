"""`toolforge_sdk`: the one module a tool may import from ToolForge, as `from toolforge_sdk import ToolError`.

The host writes this file into every sandbox as `/workspace/.tf/toolforge_sdk.py`, so it must stay standard library only and must
not import `toolforge`. It is also importable on the host (`toolforge.sdk`), which is how its tests run. Nothing here is a
security boundary: a tool runs arbitrary code in its sandbox anyway. The helpers exist so that an honest tool gets the file
layout right and so that a mistake produces a message the caller can act on.

Proven by `tests/test_sdk.py`. The contract is in `docs/guide/package-format.md`, section "How a tool runs".
"""

from __future__ import annotations

import os
import posixpath

__all__ = ["ToolError", "input_path", "output_path"]

_WORKSPACE = "/workspace"
_INPUTS = "inputs"
_OUTPUTS = "outputs"
_LISTED = 20  # how many provided file names an "unknown input" message lists


class ToolError(Exception):
    """An expected failure: the tool understood the call and is refusing it. The message goes to the caller.

    Raise it for problems the caller can fix (a file that is not a CSV, a missing column). Any other exception is reported as a
    crash, which counts against the tool and fails verification.
    """


def _configure(workspace: str) -> None:
    """Called by the harness before the tool runs; it points the helpers at the sandbox's workspace (always /workspace there)."""
    global _WORKSPACE
    _WORKSPACE = workspace


def _relative(name: object, what: str) -> str:
    """`name` as a clean relative POSIX path, or ToolError. No absolute paths, no `..`, no backslashes."""
    if not isinstance(name, str) or not name.strip():
        raise ToolError(f"{what} needs a file name as text, for example {what}('data.csv')")
    if "\x00" in name or "\\" in name:
        raise ToolError(f"{name[:60]!r} is not a plain relative file name (no backslashes or NUL)")
    if name.startswith("/"):
        raise ToolError(f"{name[:60]!r} is absolute; give a name relative to the {what.split('_')[0]} folder")
    for segment in name.split("/"):
        if segment in ("", ".", ".."):
            raise ToolError(f"{name[:60]!r} is not a plain relative file name (no empty, . or .. parts)")
    return name


def _listing(root: str) -> str:
    names: list[str] = []
    for folder, _, files in os.walk(root):
        for file in files:
            names.append(posixpath.relpath(posixpath.join(folder, file), root))
    names.sort()
    if not names:
        return "no files were provided"
    shown = ", ".join(repr(n) for n in names[:_LISTED])
    return f"the caller provided: {shown}" + (
        f" and {len(names) - _LISTED} more" if len(names) > _LISTED else ""
    )


def input_path(name: str) -> str:
    """The absolute path of a file the caller provided for this call, for example `input_path("data.csv")`.

    Caller files live under `/workspace/inputs/`. The name is relative to that folder, and one leading `inputs/` is accepted and
    means the same file, so the path a caller wrote in the arguments (`inputs/data.csv`) works too. Raises `ToolError`, with the
    names that were provided, when there is no such file.
    """
    relative = _relative(name, "input_path")
    if relative.startswith(_INPUTS + "/"):
        relative = relative[len(_INPUTS) + 1 :]
    root = posixpath.join(_WORKSPACE, _INPUTS)
    path = posixpath.join(root, relative)
    inside = os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)
    if not inside or not os.path.isfile(path):
        raise ToolError(f"there is no input file named {name[:60]!r}; {_listing(root)}")
    return path


def output_path(name: str) -> str:
    """The absolute path where the tool may write a file, `/workspace/outputs/<name>`. The folders are created; the file is not.

    Not delivered yet: files written here are discarded with the sandbox, because returning them to the caller belongs to a
    later patch (P14). Today a tool returns everything through its result.
    """
    relative = _relative(name, "output_path")
    path = posixpath.join(_WORKSPACE, _OUTPUTS, relative)
    os.makedirs(posixpath.dirname(path), exist_ok=True)
    return path
