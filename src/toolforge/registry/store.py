"""The blob store: frozen tool packages as content-addressed files, `blobs/<sha256>.tar.gz`.

Security properties, each proved in `tests/test_registry_store.py`:

* A blob is named by the SHA-256 of its bytes and `get` re-hashes what it reads, so a file that was truncated, flipped or
  swapped on disk is reported (`blob_corrupt`) instead of being handed to a sandbox.
* An address is exactly 64 lower-case hex characters. Anything else is refused before a path is built, so a caller cannot
  reach outside the blob directory with `..` or an absolute path.
* A write goes to a temporary file in the same directory and is moved into place with `os.replace`, so a reader never sees
  a half-written blob and a crash leaves at most a stray `.tmp-*` file that no address can name.

The store does not look inside a blob. What the bytes are (a tar made by `ToolPackage.to_tar`, or the archive that the build
sandbox froze) is the business of the caller.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path

from toolforge.errors import RegistryError

_SHA256 = re.compile(r"[0-9a-f]{64}")
SUFFIX = ".tar.gz"


class BlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, sha: str) -> Path:
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise RegistryError(
                "a blob address is the 64 lower-case hex characters of a SHA-256",
                code="blob_address_invalid",
                hint="use the sha that `put` returned",
            )
        return self.root / f"{sha}{SUFFIX}"

    def put(self, data: bytes) -> str:
        """Store `data` and return its SHA-256. Storing the same bytes again changes nothing."""
        if not isinstance(data, (bytes, bytearray)):
            raise RegistryError("a blob must be bytes", code="blob_invalid")
        data = bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        target = self._path(sha)
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
            return sha  # already stored and intact; a damaged copy falls through and is replaced
        descriptor, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
        return sha

    def get(self, sha: str) -> bytes:
        """The bytes stored under `sha`, verified. Raises `RegistryError` when missing or when the content no longer matches."""
        path = self._path(sha)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise RegistryError(
                f"blob {sha[:12]} is not in the store",
                code="blob_missing",
                hint="the version was never frozen, or the blobs directory was changed",
            ) from None
        if hashlib.sha256(data).hexdigest() != sha:
            raise RegistryError(
                f"blob {sha[:12]} is corrupt: its content no longer matches its address",
                code="blob_corrupt",
                hint="do not run this version; restore the blob from a backup or submit the tool again",
            )
        return data

    def exists(self, sha: str) -> bool:
        """Is there a file for this address? A malformed address is simply not present. Does not re-hash; `get` does."""
        try:
            return self._path(sha).is_file()
        except RegistryError:
            return False

    def size(self, sha: str) -> int:
        """The stored size in bytes (a stat, no hashing)."""
        try:
            return self._path(sha).stat().st_size
        except FileNotFoundError:
            raise RegistryError(f"blob {sha[:12]} is not in the store", code="blob_missing") from None
