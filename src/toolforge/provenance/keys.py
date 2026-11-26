"""The operator's signing key and the list of keys they trust (`$TOOLFORGE_HOME/keys/`).

```
keys/signing.key         the private key: one line, mode 0600, never printed, exported, logged or put in a ledger
keys/signing.pub         the matching public key: one line, safe to hand to anyone
keys/trusted/<fp>.pub    public keys the operator chose to trust, one file each
```

Security properties, each proved in `tests/test_provenance_keys.py`:

* The private key is created with mode 0600 inside a 0700 folder, never overwritten (`init` refuses when a key exists), and read only
  if no group or other permission bit is set (on a filesystem that has permissions). No message, return value or file other than
  `signing.key` ever holds its text.
* A fingerprint is the first 128 bits of the SHA-256 of the raw public key, as 32 lower-case hex characters. Trust is decided by the
  fingerprint computed from the key's own bytes, never by a file name or a label.
* A key file is one short line (`toolforge-ed25519-public-v1 <base64> [label]`) that must decode to exactly 32 bytes. Handing `trust` a
  private key file is refused with a warning rather than copied.

Trust changes only what a person is told about a signer. It never changes a tool's status and never skips a gate (`attest`, `bundle`).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from toolforge import textsafety
from toolforge.errors import ProvenanceError

KEYS_DIR = "keys"
PRIVATE_NAME = "signing.key"
PUBLIC_NAME = "signing.pub"
TRUSTED_DIR = "trusted"
PUBLIC_TAG = "toolforge-ed25519-public-v1"
PRIVATE_TAG = "toolforge-ed25519-private-v1"
MAX_KEY_FILE_BYTES = 2048
MAX_LABEL_CHARS = 60
MAX_TRUSTED_KEYS = 500
FINGERPRINT_HEX = 32
_FINGERPRINT = re.compile(rf"[0-9a-f]{{{FINGERPRINT_HEX}}}")

OWN, TRUSTED, UNTRUSTED = "own", "trusted", "untrusted"


@dataclass(frozen=True)
class PublicKey:
    """A public key: its 32 raw bytes, its fingerprint and an optional short label (text, shown after cleaning)."""

    raw: bytes
    label: str | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.raw)

    def line(self) -> str:
        """The one-line file form."""
        parts = [PUBLIC_TAG, base64.b64encode(self.raw).decode("ascii")]
        if self.label:
            parts.append(self.label)
        return " ".join(parts) + "\n"


def fingerprint(raw_public_key: bytes) -> str:
    """32 hex characters: the first 128 bits of the SHA-256 of the raw public key."""
    return hashlib.sha256(raw_public_key).hexdigest()[:FINGERPRINT_HEX]


def is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and _FINGERPRINT.fullmatch(value) is not None


def keys_dir(home: str | os.PathLike[str]) -> Path:
    return Path(home) / KEYS_DIR


def _label(text: str | None) -> str | None:
    if text is None:
        return None
    clean = textsafety.sanitize(" ".join(str(text).split()), max_chars=MAX_LABEL_CHARS).strip()
    return clean or None


def parse_public_key(text: str) -> PublicKey:
    """Read one public key line. Raises `ProvenanceError` (`key_invalid`) for anything else, and names a private key as such."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ProvenanceError(
            "a public key file holds exactly one line",
            code="key_invalid",
            hint="pass the signing.pub file of the other operator",
        )
    parts = lines[0].split()
    if parts[0] == PRIVATE_TAG:
        raise ProvenanceError(
            "this is a private key file; a private key must never leave its owner",
            code="key_is_private",
            hint="ask for the other operator's signing.pub, which is the public half, and keep your own private key to yourself",
        )
    if parts[0] != PUBLIC_TAG or len(parts) < 2:
        raise ProvenanceError(
            f"this is not a ToolForge public key (expected a line that starts with {PUBLIC_TAG})",
            code="key_invalid",
            hint="the file is created by `toolforge keys init` as keys/signing.pub",
        )
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise ProvenanceError("the key in this file is not a 32-byte Ed25519 public key", code="key_invalid")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise ProvenanceError(
            "the key in this file is not a valid Ed25519 public key", code="key_invalid"
        ) from None
    return PublicKey(raw, _label(" ".join(parts[2:])))


def _read_small(path: Path, what: str) -> str:
    """Read a key file without following a link, at most `MAX_KEY_FILE_BYTES`. The text is never put in a message."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(MAX_KEY_FILE_BYTES + 1)
    except FileNotFoundError:
        raise ProvenanceError(f"{what} does not exist", code="key_missing") from None
    except OSError as exc:
        raise ProvenanceError(
            f"{what} cannot be read ({exc.strerror or type(exc).__name__})", code="key_unreadable"
        ) from None
    if len(data) > MAX_KEY_FILE_BYTES:
        raise ProvenanceError(
            f"{what} is larger than {MAX_KEY_FILE_BYTES} bytes, so it is not a key", code="key_invalid"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ProvenanceError(f"{what} is not text, so it is not a key", code="key_invalid") from None


def _write_replace(path: Path, text: str, mode: int) -> None:
    """Write `text` to a temporary file in the same folder and move it into place, so a reader never sees half a file."""
    descriptor, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def _create_exclusive(path: Path, text: str, mode: int) -> None:
    """Create `path` with `mode` from the first byte and fail with `FileExistsError` when it is there. Never replaces a file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def _folder(home: str | os.PathLike[str], *parts: str) -> Path:
    """`keys/` (and a folder inside it), always 0700: `mkdir(parents=True)` would give a parent the default mode, and `trust` may run before `init`."""
    root = keys_dir(home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)
    folder = root.joinpath(*parts)
    folder.mkdir(exist_ok=True, mode=0o700)
    return folder


# --- The operator's own key ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyInfo:
    fingerprint: str
    public_path: Path
    created: bool


def init_keys(home: str | os.PathLike[str]) -> KeyInfo:
    """Create the signing key pair. Refuses when a private key already exists: replacing it would orphan every bundle it signed."""
    folder = _folder(home)
    private_path, public_path = folder / PRIVATE_NAME, folder / PUBLIC_NAME
    if private_path.exists() or private_path.is_symlink():
        raise ProvenanceError(
            f"a signing key already exists in {folder}",
            code="key_exists",
            hint="`toolforge keys show` prints its fingerprint; to start over, move the keys folder aside yourself",
        )
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    private_text = f"{PRIVATE_TAG} {base64.b64encode(seed).decode('ascii')}\n"
    try:
        _create_exclusive(private_path, private_text, 0o600)
    except FileExistsError:
        raise ProvenanceError(
            f"a signing key already exists in {folder}",
            code="key_exists",
            hint="`toolforge keys show` prints its fingerprint",
        ) from None
    _write_replace(public_path, PublicKey(raw_public).line(), 0o644)
    return KeyInfo(fingerprint(raw_public), public_path, True)


def _check_private_mode(path: Path) -> None:
    if os.name != "posix":
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ProvenanceError(
            f"the signing key file can be read by other users (mode {mode:04o}); it must be 0600",
            code="key_permissions",
            hint=f"run: chmod 600 {path}   (on WSL2 keep $TOOLFORGE_HOME on the Linux filesystem, not under /mnt/c)",
        )


def load_private_key(home: str | os.PathLike[str]) -> tuple[Ed25519PrivateKey, bytes]:
    """The signing key and its raw public key. Raises `ProvenanceError` (`no_signing_key`, `key_permissions`, `key_invalid`)."""
    path = keys_dir(home) / PRIVATE_NAME
    if not path.is_file():
        raise ProvenanceError(
            "there is no signing key yet",
            code="no_signing_key",
            hint="create one with `toolforge keys init`",
        )
    _check_private_mode(path)
    parts = _read_small(path, "the signing key file").split()
    try:
        seed = (
            base64.b64decode(parts[1], validate=True) if len(parts) == 2 and parts[0] == PRIVATE_TAG else b""
        )
        if len(seed) != 32:
            raise ValueError
        key = Ed25519PrivateKey.from_private_bytes(seed)
    except (ValueError, binascii.Error):
        raise ProvenanceError(
            "the signing key file is damaged, so it cannot be used",
            code="key_invalid",
            hint="restore keys/signing.key from a backup; ToolForge cannot recreate it",
        ) from None
    return key, key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def own_public_key(home: str | os.PathLike[str]) -> PublicKey | None:
    """The public half of the operator's own key, or None when `keys init` has not run. Reads only the public file."""
    path = keys_dir(home) / PUBLIC_NAME
    if not path.is_file():
        return None
    return parse_public_key(_read_small(path, "the public key file"))


# --- Trusted keys ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedKey:
    fingerprint: str
    label: str | None
    kind: str  # OWN or TRUSTED


def trust_key(
    home: str | os.PathLike[str], source: str | os.PathLike[str], label: str | None = None
) -> TrustedKey:
    """Add the public key in the file `source` to the trusted list. Returns it. Trusting a key twice only updates its label."""
    key = parse_public_key(_read_small(Path(source), "the key file"))
    folder = _folder(home, TRUSTED_DIR)
    if (
        sum(1 for _ in folder.glob("*.pub")) >= MAX_TRUSTED_KEYS
        and not (folder / f"{key.fingerprint}.pub").exists()
    ):
        raise ProvenanceError(f"the trusted list already holds {MAX_TRUSTED_KEYS} keys", code="too_many_keys")
    chosen = _label(label) if label is not None else key.label
    stored = PublicKey(key.raw, chosen)
    _write_replace(folder / f"{stored.fingerprint}.pub", stored.line(), 0o644)
    return TrustedKey(stored.fingerprint, stored.label, TRUSTED)


def untrust_key(home: str | os.PathLike[str], fp: str) -> bool:
    """Remove a key from the trusted list. False when it was not there. The operator's own key cannot be removed here."""
    if not is_fingerprint(fp):
        raise ProvenanceError(
            f"a fingerprint is {FINGERPRINT_HEX} lower-case hex characters",
            code="bad_fingerprint",
            hint="`toolforge keys list` shows them",
        )
    path = keys_dir(home) / TRUSTED_DIR / f"{fp}.pub"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def list_trusted(home: str | os.PathLike[str]) -> list[TrustedKey]:
    """Own key first, then every trusted key by fingerprint. A file that is not a valid key is ignored; the fingerprint comes from the key's bytes."""
    found: dict[str, TrustedKey] = {}
    own = None
    with contextlib.suppress(ProvenanceError):
        own = own_public_key(home)
    if own is not None:
        found[own.fingerprint] = TrustedKey(own.fingerprint, own.label, OWN)
    folder = keys_dir(home) / TRUSTED_DIR
    if folder.is_dir():
        for path in sorted(folder.glob("*.pub"))[:MAX_TRUSTED_KEYS]:
            try:
                key = parse_public_key(_read_small(path, "a trusted key file"))
            except ProvenanceError:
                continue
            found.setdefault(key.fingerprint, TrustedKey(key.fingerprint, key.label, TRUSTED))
    return list(found.values())


def trust_of(home: str | os.PathLike[str], fp: str) -> TrustedKey | None:
    """How this operator treats a signer: their own key, a trusted key, or None (nobody they chose)."""
    return next((key for key in list_trusted(home) if key.fingerprint == fp), None)


__all__ = [
    "KEYS_DIR",
    "OWN",
    "TRUSTED",
    "UNTRUSTED",
    "KeyInfo",
    "PublicKey",
    "TrustedKey",
    "fingerprint",
    "init_keys",
    "is_fingerprint",
    "keys_dir",
    "list_trusted",
    "load_private_key",
    "own_public_key",
    "parse_public_key",
    "trust_key",
    "trust_of",
    "untrust_key",
]
