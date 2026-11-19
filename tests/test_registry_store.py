"""The blob store: content addressing, atomic writes, corruption detection, and refusal of hostile addresses."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from toolforge.errors import RegistryError
from toolforge.registry.store import BlobStore

DATA = b"a frozen package"


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def test_put_returns_the_sha_and_get_returns_the_bytes(store: BlobStore) -> None:
    sha = store.put(DATA)
    assert sha == hashlib.sha256(DATA).hexdigest()
    assert store.get(sha) == DATA
    assert (store.root / f"{sha}.tar.gz").is_file()


def test_exists_and_size(store: BlobStore) -> None:
    sha = store.put(DATA)
    assert store.exists(sha) and store.size(sha) == len(DATA)
    other = hashlib.sha256(b"nothing").hexdigest()
    assert not store.exists(other)
    with pytest.raises(RegistryError) as caught:
        store.size(other)
    assert caught.value.code == "blob_missing"


def test_putting_the_same_bytes_twice_stores_one_file(store: BlobStore) -> None:
    assert store.put(DATA) == store.put(DATA)
    assert len(list(store.root.iterdir())) == 1


def test_empty_content_is_a_valid_blob(store: BlobStore) -> None:
    sha = store.put(b"")
    assert store.get(sha) == b"" and store.size(sha) == 0


def test_a_missing_blob_is_reported(store: BlobStore) -> None:
    with pytest.raises(RegistryError) as caught:
        store.get(hashlib.sha256(b"never stored").hexdigest())
    assert caught.value.code == "blob_missing"


@pytest.mark.parametrize("damage", ["flip", "truncate", "append", "swap", "empty"])
def test_corruption_is_detected_on_read(store: BlobStore, damage: str) -> None:
    sha = store.put(DATA)
    path = store.root / f"{sha}.tar.gz"
    content = path.read_bytes()
    path.write_bytes(
        {
            "flip": bytes([content[0] ^ 1]) + content[1:],
            "truncate": content[:-1],
            "append": content + b"x",
            "swap": b"a different package of the same kind",
            "empty": b"",
        }[damage]
    )
    with pytest.raises(RegistryError) as caught:
        store.get(sha)
    assert caught.value.code == "blob_corrupt"
    assert "do not run" in (caught.value.hint or "")


def test_putting_again_repairs_a_damaged_blob(store: BlobStore) -> None:
    sha = store.put(DATA)
    (store.root / f"{sha}.tar.gz").write_bytes(b"damaged")
    assert store.put(DATA) == sha
    assert store.get(sha) == DATA


@pytest.mark.parametrize(
    "address",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "",
        "abc",
        "A" * 64,
        "g" * 64,
        "0" * 63,
        "0" * 65,
        "0" * 64 + "/..",
        "..\\x",
        "0" * 32 + "\x00" + "0" * 31,
    ],
)
def test_hostile_addresses_never_reach_the_filesystem(store: BlobStore, address: str) -> None:
    with pytest.raises(RegistryError) as caught:
        store.get(address)
    assert caught.value.code == "blob_address_invalid"
    assert store.exists(address) is False
    with pytest.raises(RegistryError):
        store.size(address)


def test_only_bytes_can_be_stored(store: BlobStore) -> None:
    with pytest.raises(RegistryError):
        store.put("text, not bytes")  # type: ignore[arg-type]


def test_a_failed_write_leaves_no_blob_and_no_temp_file(
    store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        store.put(DATA)
    assert list(store.root.iterdir()) == []


def test_a_reader_never_sees_a_partial_blob(store: BlobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """While `put` is between writing the temp file and the rename, the address does not exist yet."""
    sha = hashlib.sha256(DATA).hexdigest()
    seen: list[bool] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        seen.append(store.exists(sha))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.put(DATA)
    assert seen == [False] and store.exists(sha)


def test_blobs_are_private_to_the_owner(store: BlobStore) -> None:
    sha = store.put(DATA)
    assert stat.S_IMODE((store.root / f"{sha}.tar.gz").stat().st_mode) == 0o600
