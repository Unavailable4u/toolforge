"""Reading, writing and archiving tool packages on disk. Standard library only; no knowledge of the manifest model.

Security properties (named so the tests that prove them are easy to find):

* Reading a directory never follows a symbolic link, never opens a special file, never reads more than a fixed number of
  bytes per file and never walks more than a fixed number of entries (`tests/test_package_dir.py`).
* Writing a package refuses any path that is not a plain relative path and never writes into a directory that already
  holds something (`tests/test_package_dir.py`).
* `build_tar` is deterministic: the same entries always give the same bytes, whatever the order they were given in or the
  machine's locale and clock (`tests/test_package_tar.py`). The package hash (`code_sha`) is the hash of those bytes.

Nothing here executes package code. Problems found while reading are returned as `(path, code, message)` tuples so the
caller can turn them into `Issue`s; the codes are catalogued in `toolforge.models.package.ISSUE_CODES`.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from toolforge.errors import ToolForgeError

MANIFEST_NAMES = ("manifest.json", "manifest.yaml", "manifest.yml")
MAX_FILES = 40
MAX_FILE_BYTES = 64 * 1024
MAX_PATH_CHARS = 200
ALLOWED_EXTENSIONS = (".py", ".json", ".yaml", ".txt", ".md", ".csv")

# Reading limits for a directory on disk. A package may hold at most MAX_FILES files, so these only bound the work done on
# a hostile directory before it is rejected.
MAX_WALK_ENTRIES = 400
MAX_DIR_DEPTH = 8
MAX_MANIFEST_READ_BYTES = 1024 * 1024

# Tool caches that appear next to a package when its author runs the tests; they are never part of the package.
IGNORED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
IGNORED_FILES = frozenset({".DS_Store"})

Problem = tuple[str, str, str]  # (path, code, message)


def check_rel_path(path: object) -> str | None:
    """Why `path` is not a plain, normalised, relative path, or None when it is fine."""
    if not isinstance(path, str) or not path:
        return "must be non-empty text"
    if len(path) > MAX_PATH_CHARS:
        return f"is longer than {MAX_PATH_CHARS} characters"
    for char in path:
        if unicodedata.category(char)[0] == "C":
            return "contains a control or invisible character"
    if "\\" in path:
        return "uses a backslash; use forward slashes"
    if path.startswith("/"):
        return "is absolute (starts with /)"
    for segment in path.split("/"):
        if segment == "":
            return "has an empty segment (a doubled or trailing /)"
        if segment in {".", ".."}:
            return "contains . or .., so it is not normalised"
        if segment.startswith("."):
            return "has a segment that starts with a dot"
    return None


def text_problem(text: str) -> tuple[str, str] | None:
    """`(code, message)` when `text` cannot be stored as a package file, or None. Packages are text only."""
    if "\x00" in text:
        return "file.nul", "contains a NUL character; package files are text only"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return (
            "file.encoding",
            "contains characters that are not valid Unicode text (for example a lone surrogate)",
        )
    return None


def canonical_manifest_bytes(manifest_json: Mapping[str, Any]) -> bytes:
    """The manifest as stored in the archive: sorted keys, two-space indent, UTF-8, trailing newline.

    Canonical on purpose, so a package read from a directory and the same package submitted as JSON hash identically.
    """
    text = json.dumps(manifest_json, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def build_tar(entries: Mapping[str, bytes]) -> bytes:
    """A deterministic, uncompressed tar: sorted names, mtime 0, uid and gid 0, mode 0644, GNU format, UTF-8 names."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT, encoding="utf-8") as archive:
        for name in sorted(entries):
            data = entries[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@dataclass
class LoadedDir:
    """What a package directory held. `manifest` is the parsed JSON (any shape) or None when it could not be read."""

    manifest: Any = None
    files: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    skipped: set[str] = field(default_factory=set)
    manifest_explained: bool = False  # True when `problems` already says why `manifest` is None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def read_package_dir(root: str | os.PathLike[str]) -> LoadedDir:
    """Read a package directory without executing anything in it. Bad content becomes `problems`, not exceptions.

    Only a missing or unreadable root raises, because that is a usage error rather than bad package content.
    """
    base = Path(root)
    if not base.is_dir():
        raise ToolForgeError(
            f"{root} is not a directory",
            code="package_not_found",
            hint="pass the folder that contains manifest.json",
        )
    loaded = LoadedDir()
    manifest_text: str | None = None
    seen = 0
    stack: list[tuple[str, str, int]] = [("", str(base), 0)]
    while stack:
        rel_dir, abs_dir, depth = stack.pop()
        try:
            with os.scandir(abs_dir) as scan:
                entries = sorted(scan, key=lambda item: item.name)
        except OSError as exc:
            loaded.problems.append(
                (rel_dir or ".", "file.path", f"cannot read this directory ({exc.strerror})")
            )
            continue
        for entry in entries:
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                loaded.problems.append(
                    (
                        "",
                        "files.too_many",
                        f"the directory holds more than {MAX_WALK_ENTRIES} entries; stopped reading",
                    )
                )
                stack.clear()
                break
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_symlink():
                loaded.problems.append(
                    (rel, "file.path", "is a symbolic link; packages hold regular files only")
                )
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in IGNORED_DIRS:
                    continue
                if depth + 1 > MAX_DIR_DEPTH:
                    loaded.problems.append(
                        (rel, "file.path", f"is nested deeper than {MAX_DIR_DEPTH} folders")
                    )
                    continue
                stack.append((rel, entry.path, depth + 1))
                continue
            if entry.name in IGNORED_FILES:
                continue
            if not entry.is_file(follow_symlinks=False):
                loaded.problems.append(
                    (rel, "file.path", "is not a regular file (a device, socket or pipe?)")
                )
                continue
            is_manifest = rel in MANIFEST_NAMES
            text = _read_text(entry, rel, MAX_MANIFEST_READ_BYTES if is_manifest else MAX_FILE_BYTES, loaded)
            if text is None:
                if is_manifest:
                    loaded.manifest_explained = True
                continue
            if is_manifest:
                manifest_text = text
            else:
                loaded.files[rel] = text
    if manifest_text is not None:
        # Try parsing as JSON first, then YAML
        try:
            loaded.manifest = json.loads(manifest_text, parse_constant=_reject_constant)
        except json.JSONDecodeError:
            # Try YAML if JSON fails
            try:
                loaded.manifest = yaml.safe_load(manifest_text)
                if loaded.manifest is None:
                    loaded.manifest = {}
            except yaml.YAMLError as exc:
                loaded.problems.append(
                    (
                        MANIFEST_NAMES[0],
                        "manifest.invalid_json",
                        f"is not valid JSON or YAML: {exc}",
                    )
                )
                loaded.manifest_explained = True
        except (ValueError, RecursionError) as exc:
            loaded.problems.append(
                (MANIFEST_NAMES[0], "manifest.invalid_json", f"is not valid JSON: {_first_line(exc)}")
            )
            loaded.manifest_explained = True
    return loaded


def _first_line(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:120]


def _read_text(entry: os.DirEntry[str], rel: str, limit: int, loaded: LoadedDir) -> str | None:
    """Read one regular file as UTF-8 without translating line endings, or record why not and return None."""
    is_manifest = rel in MANIFEST_NAMES
    too_large = (
        ("package.too_large", f"is larger than {limit // 1024} KB")
        if is_manifest
        else (
            "file.too_large",
            f"is larger than {limit // 1024} KB",
        )
    )
    try:
        info = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            loaded.problems.append((rel, "file.path", "is not a regular file"))
            loaded.skipped.add(rel)
            return None
        if info.st_size > limit:
            loaded.problems.append((rel, *too_large))
            loaded.skipped.add(rel)
            return None
        fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        loaded.problems.append((rel, "file.path", f"cannot be read ({exc.strerror})"))
        loaded.skipped.add(rel)
        return None
    if len(data) > limit:
        loaded.problems.append((rel, *too_large))
        loaded.skipped.add(rel)
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        loaded.problems.append((rel, "file.encoding", "is not valid UTF-8 text; package files are text only"))
        loaded.skipped.add(rel)
        return None


def write_package_dir(dest: str | os.PathLike[str], manifest_text: str, files: Mapping[str, str]) -> Path:
    """Write a package into `dest`, which must not exist or must be empty. Returns the directory.

    Every path is checked first, so a hostile `files` mapping cannot write outside `dest`.
    """
    target = Path(dest)
    names = {MANIFEST_NAME: manifest_text, **files}
    for name, text in names.items():
        problem = check_rel_path(name)
        if problem is not None or not isinstance(text, str) or text_problem(text) is not None:
            raise ToolForgeError(
                f"refusing to write {name!r}: {problem or 'its content is not text'}",
                code="unsafe_path",
                hint="packages hold plain relative paths and text content only",
            )
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ToolForgeError(
            f"{target} already exists and is not an empty directory",
            code="package_exists",
            hint="choose another name or folder, or remove the existing one yourself",
        )
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    for name in sorted(names):
        path = (root / name).resolve()
        if not path.is_relative_to(root):  # defence in depth: check_rel_path already forbids this
            raise ToolForgeError(f"refusing to write outside {target}", code="unsafe_path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(names[name].encode("utf-8"))
    return target
