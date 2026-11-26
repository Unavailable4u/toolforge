"""The signing key and the trusted list (`toolforge.provenance.keys`): permissions, refusals, fingerprints, and that trust is decided by the
key's own bytes. Each property named in the module's docstring is one test here."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from toolforge.errors import ProvenanceError
from toolforge.provenance import keys

posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes need a POSIX filesystem")


def _public_file(tmp_path: Path, name: str = "other.pub", label: str | None = None) -> tuple[Path, bytes]:
    raw = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    path = tmp_path / name
    path.write_text(keys.PublicKey(raw, label).line(), encoding="utf-8")
    return path, raw


def _seed_text(home: Path) -> str:
    """The base64 seed inside signing.key: the one string that must appear nowhere else."""
    return (keys.keys_dir(home) / keys.PRIVATE_NAME).read_text(encoding="utf-8").split()[1]


def _code(call, *args) -> str:
    with pytest.raises(ProvenanceError) as caught:
        call(*args)
    return caught.value.code


# --- Creating the key ---------------------------------------------------------------------------------------------------------


@posix_only
def test_init_creates_the_private_key_with_mode_0600_in_a_0700_folder(tmp_path: Path) -> None:
    info = keys.init_keys(tmp_path)
    folder = keys.keys_dir(tmp_path)
    assert stat.S_IMODE((folder / keys.PRIVATE_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(info.public_path.stat().st_mode) == 0o644
    assert info.created and info.public_path == folder / keys.PUBLIC_NAME


@posix_only
def test_the_keys_folder_is_0700_even_when_trust_ran_before_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = os.umask(0o022)
    try:
        source, _ = _public_file(tmp_path)
        keys.trust_key(tmp_path, source)  # creates keys/ first
        keys.init_keys(tmp_path)
    finally:
        os.umask(previous)
    folder = keys.keys_dir(tmp_path)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / keys.TRUSTED_DIR).stat().st_mode) == 0o700


@posix_only
def test_init_tightens_a_keys_folder_that_was_left_open(tmp_path: Path) -> None:
    folder = keys.keys_dir(tmp_path)
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    keys.init_keys(tmp_path)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_init_refuses_to_replace_an_existing_key_and_leaves_it_untouched(tmp_path: Path) -> None:
    keys.init_keys(tmp_path)
    before = (keys.keys_dir(tmp_path) / keys.PRIVATE_NAME).read_bytes()
    assert _code(keys.init_keys, tmp_path) == "key_exists"
    assert (keys.keys_dir(tmp_path) / keys.PRIVATE_NAME).read_bytes() == before


def test_init_refuses_even_when_the_private_key_is_a_dangling_link(tmp_path: Path) -> None:
    folder = keys.keys_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / keys.PRIVATE_NAME).symlink_to(tmp_path / "nowhere")
    assert _code(keys.init_keys, tmp_path) == "key_exists"
    assert not (tmp_path / "nowhere").exists()  # init did not write through the link


def test_the_private_key_text_is_in_signing_key_and_nowhere_else(tmp_path: Path) -> None:
    keys.init_keys(tmp_path)
    other, _ = _public_file(tmp_path)
    keys.trust_key(tmp_path, other, "friend")
    seed = _seed_text(tmp_path)
    holders = [
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        if p.is_file() and seed.encode("ascii") in p.read_bytes()
    ]
    assert holders == ["keys/signing.key"]


def test_the_public_file_holds_the_public_half_of_the_private_key(tmp_path: Path) -> None:
    info = keys.init_keys(tmp_path)
    private, raw_public = keys.load_private_key(tmp_path)
    own = keys.own_public_key(tmp_path)
    assert own is not None and own.raw == raw_public and own.fingerprint == info.fingerprint
    assert private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) == raw_public


# --- Reading the private key --------------------------------------------------------------------------------------------------


def test_load_without_a_key_says_how_to_create_one(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError) as caught:
        keys.load_private_key(tmp_path)
    assert caught.value.code == "no_signing_key" and "keys init" in str(caught.value)


@posix_only
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_a_private_key_that_others_can_read_is_not_used(tmp_path: Path, mode: int) -> None:
    keys.init_keys(tmp_path)
    path = keys.keys_dir(tmp_path) / keys.PRIVATE_NAME
    path.chmod(mode)
    with pytest.raises(ProvenanceError) as caught:
        keys.load_private_key(tmp_path)
    assert caught.value.code == "key_permissions" and "chmod 600" in str(caught.value)
    assert _seed_text(tmp_path) not in str(caught.value)


@posix_only
def test_a_private_key_readable_only_by_its_owner_loads_whatever_the_owner_bits_are(tmp_path: Path) -> None:
    keys.init_keys(tmp_path)
    (keys.keys_dir(tmp_path) / keys.PRIVATE_NAME).chmod(0o400)
    private, _ = keys.load_private_key(tmp_path)
    assert isinstance(private, Ed25519PrivateKey)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "toolforge-ed25519-private-v1\n",
        "toolforge-ed25519-private-v1 not-base64!!\n",
        f"toolforge-ed25519-private-v1 {base64.b64encode(b'short').decode()}\n",
        f"{keys.PUBLIC_TAG} {base64.b64encode(bytes(32)).decode()}\n",
        "toolforge-ed25519-private-v1 " + "A" * 5000 + "\n",
    ],
)
def test_a_damaged_private_key_file_is_refused_without_echoing_it(tmp_path: Path, content: str) -> None:
    keys.init_keys(tmp_path)
    path = keys.keys_dir(tmp_path) / keys.PRIVATE_NAME
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ProvenanceError) as caught:
        keys.load_private_key(tmp_path)
    assert caught.value.code == "key_invalid"
    assert content.strip() == "" or content.split()[-1] not in str(caught.value)


@posix_only
def test_a_private_key_that_is_a_link_is_not_followed(tmp_path: Path) -> None:
    keys.init_keys(tmp_path)
    path = keys.keys_dir(tmp_path) / keys.PRIVATE_NAME
    real = tmp_path / "elsewhere.key"
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(ProvenanceError) as caught:
        keys.load_private_key(tmp_path)
    assert caught.value.code == "key_unreadable"


# --- Fingerprints --------------------------------------------------------------------------------------------------------------


def test_a_fingerprint_is_the_first_128_bits_of_the_sha256_of_the_raw_key() -> None:
    raw = bytes(range(32))
    assert keys.fingerprint(raw) == hashlib.sha256(raw).hexdigest()[:32]
    assert len(keys.fingerprint(raw)) == keys.FINGERPRINT_HEX == 32


@pytest.mark.parametrize(
    ("value", "valid"),
    [("a" * 32, True), ("0123456789abcdef" * 2, True), ("A" * 32, False), ("a" * 31, False), ("a" * 33, False),
     ("a" * 31 + "\n", False), ("g" * 32, False), (None, False), (b"a" * 32, False), (12, False)],
)  # fmt: skip
def test_is_fingerprint_is_exact(value: object, valid: bool) -> None:
    assert keys.is_fingerprint(value) is valid


# --- Reading a public key line ------------------------------------------------------------------------------------------------


def test_a_public_key_line_round_trips_with_its_label() -> None:
    raw = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key = keys.parse_public_key(keys.PublicKey(raw, "alice laptop").line())
    assert key.raw == raw and key.label == "alice laptop" and key.fingerprint == keys.fingerprint(raw)


def test_a_label_is_cleaned_and_clipped() -> None:
    raw = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    line = f"{keys.PUBLIC_TAG} {base64.b64encode(raw).decode()} evil\x1b[31m red\x07 {'x' * 200}\n"
    label = keys.parse_public_key(line).label
    assert label is not None and len(label) <= keys.MAX_LABEL_CHARS
    assert "\x1b" not in label and "\x07" not in label


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        f"{keys.PUBLIC_TAG}\n",
        "not-a-key abc\n",
        f"{keys.PUBLIC_TAG} !!!notbase64!!!\n",
        f"{keys.PUBLIC_TAG} {base64.b64encode(bytes(31)).decode()}\n",
        f"{keys.PUBLIC_TAG} {base64.b64encode(bytes(33)).decode()}\n",
        f"{keys.PUBLIC_TAG} {base64.b64encode(bytes(32)).decode()}\n{keys.PUBLIC_TAG} {base64.b64encode(bytes(32)).decode()}\n",
    ],
)
def test_anything_that_is_not_one_public_key_line_is_refused(text: str) -> None:
    assert _code(keys.parse_public_key, text) == "key_invalid"


def test_a_private_key_is_named_as_such_and_never_copied_into_the_message(tmp_path: Path) -> None:
    keys.init_keys(tmp_path)
    text = (keys.keys_dir(tmp_path) / keys.PRIVATE_NAME).read_text(encoding="utf-8")
    with pytest.raises(ProvenanceError) as caught:
        keys.parse_public_key(text)
    assert caught.value.code == "key_is_private"
    assert text.split()[1] not in str(caught.value)


# --- Trusting other keys -------------------------------------------------------------------------------------------------------


def test_trust_stores_the_key_under_its_own_fingerprint(tmp_path: Path) -> None:
    source, raw = _public_file(tmp_path, "whatever-name.txt", "bob")
    trusted = keys.trust_key(tmp_path, source)
    assert (
        trusted.fingerprint == keys.fingerprint(raw)
        and trusted.label == "bob"
        and trusted.kind == keys.TRUSTED
    )
    assert (keys.keys_dir(tmp_path) / keys.TRUSTED_DIR / f"{trusted.fingerprint}.pub").is_file()
    found = keys.trust_of(tmp_path, trusted.fingerprint)
    assert found is not None and found.kind == keys.TRUSTED


def test_trusting_a_key_twice_only_updates_its_label(tmp_path: Path) -> None:
    source, _ = _public_file(tmp_path, label="first")
    keys.trust_key(tmp_path, source)
    again = keys.trust_key(tmp_path, source, "second")
    assert again.label == "second"
    assert [k.label for k in keys.list_trusted(tmp_path)] == ["second"]


def test_a_private_key_file_is_refused_by_trust_and_nothing_is_copied(tmp_path: Path) -> None:
    keys.init_keys(tmp_path)
    private_file = keys.keys_dir(tmp_path) / keys.PRIVATE_NAME
    with pytest.raises(ProvenanceError) as caught:
        keys.trust_key(tmp_path, private_file)
    assert caught.value.code == "key_is_private"
    assert list((keys.keys_dir(tmp_path) / keys.TRUSTED_DIR).glob("*")) == []


def test_trust_refuses_a_missing_file_a_large_file_and_a_binary_file(tmp_path: Path) -> None:
    assert _code(keys.trust_key, tmp_path, tmp_path / "missing.pub") == "key_missing"
    big = tmp_path / "big.pub"
    big.write_text("x" * (keys.MAX_KEY_FILE_BYTES + 1), encoding="utf-8")
    assert _code(keys.trust_key, tmp_path, big) == "key_invalid"
    binary = tmp_path / "bin.pub"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    assert _code(keys.trust_key, tmp_path, binary) == "key_invalid"


def test_the_trusted_list_has_a_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keys, "MAX_TRUSTED_KEYS", 2)
    first, _ = _public_file(tmp_path, "a.pub")
    second, _ = _public_file(tmp_path, "b.pub")
    third, _ = _public_file(tmp_path, "c.pub")
    keys.trust_key(tmp_path, first)
    keys.trust_key(tmp_path, second)
    assert _code(keys.trust_key, tmp_path, third) == "too_many_keys"
    keys.trust_key(tmp_path, first, "renamed")  # a key already on the list can still be updated


def test_untrust_removes_a_key_and_says_so_when_it_was_not_there(tmp_path: Path) -> None:
    source, raw = _public_file(tmp_path)
    keys.trust_key(tmp_path, source)
    fp = keys.fingerprint(raw)
    assert keys.untrust_key(tmp_path, fp) is True
    assert keys.trust_of(tmp_path, fp) is None
    assert keys.untrust_key(tmp_path, fp) is False


@pytest.mark.parametrize("bad", ["", "abc", "A" * 32, "../" + "a" * 29, "a" * 32 + ".pub"])
def test_untrust_takes_a_fingerprint_and_nothing_else(tmp_path: Path, bad: str) -> None:
    assert _code(keys.untrust_key, tmp_path, bad) == "bad_fingerprint"


def test_the_operators_own_key_cannot_be_untrusted_here(tmp_path: Path) -> None:
    info = keys.init_keys(tmp_path)
    assert keys.untrust_key(tmp_path, info.fingerprint) is False
    own = keys.trust_of(tmp_path, info.fingerprint)
    assert own is not None and own.kind == keys.OWN


def test_own_key_is_listed_first_and_keeps_its_kind_even_if_also_trusted(tmp_path: Path) -> None:
    info = keys.init_keys(tmp_path)
    other, _ = _public_file(tmp_path, label="other")
    keys.trust_key(tmp_path, other)
    keys.trust_key(tmp_path, info.public_path)  # trusting yourself changes nothing
    listed = keys.list_trusted(tmp_path)
    assert [k.kind for k in listed] == [keys.OWN, keys.TRUSTED]
    assert listed[0].fingerprint == info.fingerprint


def test_trust_is_decided_by_the_keys_bytes_not_by_its_file_name(tmp_path: Path) -> None:
    """A file called after somebody else's fingerprint does not make that somebody trusted."""
    victim_file, victim_raw = _public_file(tmp_path, "victim.pub")
    attacker_file, attacker_raw = _public_file(tmp_path, "attacker.pub")
    folder = keys.keys_dir(tmp_path) / keys.TRUSTED_DIR
    folder.mkdir(parents=True)
    (folder / f"{keys.fingerprint(victim_raw)}.pub").write_text(attacker_file.read_text(encoding="utf-8"))
    assert keys.trust_of(tmp_path, keys.fingerprint(victim_raw)) is None
    found = keys.trust_of(tmp_path, keys.fingerprint(attacker_raw))
    assert found is not None  # it is listed under what it really is
    assert victim_file.exists()


def test_damaged_files_in_the_trusted_folder_are_ignored(tmp_path: Path) -> None:
    good, raw = _public_file(tmp_path)
    keys.trust_key(tmp_path, good)
    folder = keys.keys_dir(tmp_path) / keys.TRUSTED_DIR
    (folder / "junk.pub").write_text("hello\n", encoding="utf-8")
    (folder / "binary.pub").write_bytes(b"\xff\x00")
    (folder / "huge.pub").write_text("x" * 5000, encoding="utf-8")
    assert [k.fingerprint for k in keys.list_trusted(tmp_path)] == [keys.fingerprint(raw)]


def test_a_damaged_own_public_file_does_not_stop_the_list(tmp_path: Path) -> None:
    keys.init_keys(tmp_path)
    (keys.keys_dir(tmp_path) / keys.PUBLIC_NAME).write_text("damaged\n", encoding="utf-8")
    assert keys.list_trusted(tmp_path) == []
    assert _code(keys.own_public_key, tmp_path) == "key_invalid"


def test_own_public_key_is_none_before_init(tmp_path: Path) -> None:
    assert keys.own_public_key(tmp_path) is None
    assert keys.list_trusted(tmp_path) == []
    assert keys.trust_of(tmp_path, "a" * 32) is None
