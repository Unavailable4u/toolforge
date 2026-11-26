"""`toolforge keys`, `export`, `import` and `verify-bundle`: portable tool bundles with signed provenance (optional patch S01).

These are operator commands; no MCP tool reaches them. `keys show` prints only the public fingerprint, never the private key. `verify-bundle`
changes nothing. `import` verifies the bundle and then runs the same local pipeline as `submit`, so a signature never raises a tool's status and
never skips a gate (docs/PROVENANCE.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from toolforge import cli_run
from toolforge.config import load_settings
from toolforge.errors import ProvenanceError
from toolforge.provenance import bundle, keys
from toolforge.registry import Registry
from toolforge.services.forge import Forge
from toolforge.verify.g4_fuzz import DEFAULT_INPUTS, MAX_INPUTS


def _cmd_keys_init(_: argparse.Namespace) -> int:
    settings = load_settings()
    info = keys.init_keys(settings.home)
    print(f"created a signing key; its fingerprint is {info.fingerprint}")
    print(f"public key file: {info.public_path} (give this file to people who should trust your bundles)")
    print("the private key stays in this folder with mode 0600; ToolForge never prints or exports it")
    return 0


def _cmd_keys_show(_: argparse.Namespace) -> int:
    settings = load_settings()
    own = keys.own_public_key(settings.home)
    if own is None:
        raise ProvenanceError(
            "there is no signing key yet", code="no_signing_key", hint="create one with `toolforge keys init`"
        )
    print(f"fingerprint: {own.fingerprint}")
    print(f"public key file: {keys.keys_dir(settings.home) / keys.PUBLIC_NAME}")
    return 0


def _cmd_keys_trust(args: argparse.Namespace) -> int:
    settings = load_settings()
    key = keys.trust_key(settings.home, args.file, args.label)
    print(f"trusting {key.fingerprint}" + (f" ({key.label})" if key.label else ""))
    print(
        "trust only changes what a bundle signed with this key is called when you verify it; every gate still runs on import"
    )
    return 0


def _cmd_keys_untrust(args: argparse.Namespace) -> int:
    settings = load_settings()
    if keys.untrust_key(settings.home, args.fingerprint):
        print(f"no longer trusting {args.fingerprint}")
        return 0
    print(f"{args.fingerprint} was not in the trusted list")
    return 1


def _cmd_keys_list(_: argparse.Namespace) -> int:
    settings = load_settings()
    found = keys.list_trusted(settings.home)
    if not found:
        print(
            "no keys yet: `toolforge keys init` creates yours, `toolforge keys trust <file>` adds someone else's"
        )
        return 0
    for key in found:
        what = "your own key" if key.kind == keys.OWN else "trusted"
        print(f"{key.fingerprint}  {what}" + (f"  {key.label}" if key.label else ""))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    settings = load_settings()
    with Registry.from_settings(settings) as registry:
        done = bundle.export_bundle(settings, registry, args.ref, args.output, force=args.force)
    print(done.text())
    return 0


def _cmd_verify_bundle(args: argparse.Namespace) -> int:
    settings = load_settings()
    verified = bundle.verify_bundle(args.file, settings)
    if args.json:
        print(json.dumps(verified.as_dict(), indent=2, ensure_ascii=True))
    else:
        print(verified.text())
        print("nothing was imported or changed; `toolforge import` registers it and runs every local gate")
    return 0


def _fuzz_inputs(value: int) -> int:
    if not 1 <= value <= MAX_INPUTS:
        raise ProvenanceError(
            f"--fuzz-inputs must be from 1 to {MAX_INPUTS}",
            code="bad_option",
            hint=f"the default is {DEFAULT_INPUTS}",
        )
    return value


def _cmd_import(args: argparse.Namespace) -> int:
    settings = load_settings()
    sandbox = cli_run.make_sandbox_runner(settings)
    try:
        with Registry.from_settings(settings) as registry:
            forge = Forge(
                settings,
                registry,
                sandbox,
                ledger_factory=cli_run.make_ledger,
                fuzz_inputs=_fuzz_inputs(args.fuzz_inputs),
            )
            done = bundle.import_bundle(args.file, settings, registry, forge.submit_package)
    finally:
        stop: Callable[[], object] | None = getattr(sandbox, "shutdown", None)
        if stop is not None:
            stop()
    print(json.dumps(done.as_dict(), indent=2, ensure_ascii=True) if args.json else done.text())
    return 0 if done.report.ok else 1


def register(subparsers: argparse._SubParsersAction) -> None:
    root = subparsers.add_parser(
        "keys",
        help="your signing key and the keys you trust (for portable bundles)",
        description=(
            "Create the Ed25519 key that signs the bundles you export, and keep the list of other operators' public keys that you trust. "
            "The private key never leaves $TOOLFORGE_HOME/keys (mode 0600). Trusting a key only changes how a signer is described; it never "
            "skips a gate."
        ),
    )
    sub = root.add_subparsers(dest="keys_command", metavar="<action>", required=True)
    sub.add_parser("init", help="create your signing key").set_defaults(handler=_cmd_keys_init)
    sub.add_parser("show", help="print your public fingerprint (never the private key)").set_defaults(
        handler=_cmd_keys_show
    )
    trust = sub.add_parser("trust", help="trust another operator's public key file")
    trust.add_argument("file", metavar="public-key-file", help="their signing.pub")
    trust.add_argument("--label", help="a short name to show beside the fingerprint")
    trust.set_defaults(handler=_cmd_keys_trust)
    untrust = sub.add_parser("untrust", help="stop trusting a key")
    untrust.add_argument("fingerprint", help="the 32-character fingerprint from `keys list`")
    untrust.set_defaults(handler=_cmd_keys_untrust)
    sub.add_parser("list", help="list your own key and the keys you trust").set_defaults(
        handler=_cmd_keys_list
    )

    export = subparsers.add_parser(
        "export",
        help="write a verified tool version to a signed .tfbundle file",
        description=(
            "Pack a verified version (its manifest, frozen package, verification ledger, Blast Radius report and a signed attestation) into one "
            "file another operator can check and import. Needs `toolforge keys init` first. Nothing in the registry changes."
        ),
    )
    export.add_argument("ref", metavar="name@version", help="the version, for example csv_summary@1.0.0")
    export.add_argument("-o", "--output", required=True, metavar="file.tfbundle", help="the file to write")
    export.add_argument("--force", action="store_true", help="replace the file if it exists")
    export.set_defaults(handler=_cmd_export)

    check = subparsers.add_parser(
        "verify-bundle",
        help="check a .tfbundle (structure, hashes, ledger chain, signature) without importing it",
        description=(
            "Steps 1 to 3 of an import: the archive is well formed and within limits, the hashes and the ledger's hash chain are recomputed, and the "
            "signature is checked against the key list. Prints who signed it and whether you trust them. Changes nothing."
        ),
    )
    check.add_argument("file", metavar="file.tfbundle")
    check.add_argument("--json", action="store_true", help="print the result as JSON")
    check.set_defaults(handler=_cmd_verify_bundle)

    load = subparsers.add_parser(
        "import",
        help="verify a .tfbundle, register it as a new version and run every local gate",
        description=(
            "Verify the bundle, then submit its package as import:<fingerprint> and run the full local pipeline. What the bundle says about gates is "
            "shown as information only. The version is not promoted: `toolforge promote` and `toolforge approvals` work as for any submission."
        ),
    )
    load.add_argument("file", metavar="file.tfbundle")
    load.add_argument("--json", action="store_true", help="print the result as JSON")
    load.add_argument(
        "--fuzz-inputs", type=int, default=DEFAULT_INPUTS, help="inputs for gate G4 (default 30)"
    )
    load.set_defaults(handler=_cmd_import)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
