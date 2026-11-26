"""Builders shared by the S01 tests: a home with a signing key, a verified version with real ledger files, and bundles that are
re-packed or re-signed so that one check at a time can be made to fail.

Everything here uses the real registry, the real hash-chained ledger (MiniLocker) and the real pipeline with scripted gates, so a
bundle in these tests is the same thing `toolforge export` writes. Only the sandbox is absent: gates are scripted.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

from verify_helpers import ScriptedGate, scripted_gates, valid_package

from toolforge import cli_run
from toolforge.config import Settings
from toolforge.models.package import ToolPackage
from toolforge.packaging import build_tar
from toolforge.provenance import attest, bundle, keys
from toolforge.registry import Registry, VersionRecord
from toolforge.services.forge import Forge

FIXED_NOW = "2026-11-26T12:00:00.000Z"
LOCAL = "local gate says: "


def new_home(tmp_path: Path, name: str = "home") -> tuple[Settings, Path]:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return Settings(home=home), home


def make_forge(settings: Settings, registry: Registry, gates: list[ScriptedGate] | None = None) -> Forge:
    """A `Forge` with the real ledger (files under the home) and scripted gates. G2 stores the blob, like the real one."""
    return Forge(
        settings,
        registry,
        None,
        ledger_factory=cli_run.make_ledger,
        gates=gates if gates is not None else scripted_gates(),
    )


def verified(
    settings: Settings,
    registry: Registry,
    package: ToolPackage | None = None,
    *,
    gates: list[ScriptedGate] | None = None,
    by: str = "operator",
) -> VersionRecord:
    """Submit a package through the real pipeline (scripted gates) and return its record."""
    package = package or valid_package(name="csv_sum", version="1.0.0")
    forge = make_forge(settings, registry, gates)
    report = forge.submit_package(package, by)
    record = registry.find_version(package.manifest.name, package.manifest.version)
    assert record is not None and report.status in ("verified_basic", "verified"), report.text()
    return record


def export_to(
    settings: Settings, registry: Registry, record: VersionRecord, path: Path, **kwargs: Any
) -> bundle.ExportResult:
    kwargs.setdefault("now", lambda: FIXED_NOW)
    kwargs.setdefault("image_lookup", lambda _image: None)
    return bundle.export_bundle(settings, registry, record.label, path, **kwargs)


def make_bundle(tmp_path: Path, *, name: str = "csv_sum", version: str = "1.0.0", **package_changes: Any):
    """A home with a key and one verified, exported version. Returns `(settings, home, bundle_path)`."""
    settings, home = new_home(tmp_path, "origin")
    keys.init_keys(home)
    package = valid_package(name=name, version=version, **package_changes)
    with Registry(home) as registry:
        record = verified(settings, registry, package)
        path = tmp_path / f"{name}.tfbundle"
        export_to(settings, registry, record, path)
    return settings, home, path


def members_of(path: Path) -> dict[str, bytes]:
    return bundle.read_members(path.read_bytes())


def write_members(path: Path, members: dict[str, bytes]) -> Path:
    path.write_bytes(build_tar(members))
    return path


def resigned(settings: Settings, members: dict[str, bytes], **changes: Any) -> dict[str, bytes]:
    """The same members with some attestation fields changed and the attestation signed again by the home's own key.

    The signature is therefore valid, so the bundle can only be caught by the check that looks at the field that was changed.
    """
    private, raw_public = keys.load_private_key(settings.home)
    body = json.loads(members["attestation.json"])
    for field in ("signer", "signer_key", "signature"):
        del body[field]
    body.update(changes)
    return {**members, "attestation.json": attest.canonical_json(attest.sign(body, private, raw_public))}


def edited_json(members: dict[str, bytes], member: str, **changes: Any) -> dict[str, bytes]:
    document = json.loads(members[member])
    document.update(changes)
    return {**members, member: attest.canonical_json(document)}


def raw_tar(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    """A tar made member by member, for shapes `build_tar` would never write: `(name, data, tarfile type)`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                info.linkname = "elsewhere"
                archive.addfile(info)
    return buffer.getvalue()


def tree(root: Path) -> dict[str, bytes | None]:
    """Every path under `root` with its bytes (None for a folder): to prove that a command changed nothing."""
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))
    }
