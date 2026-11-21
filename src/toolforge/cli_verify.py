"""`toolforge verify <dir>` (a dry run) and `toolforge submit <dir>` (verify and register).

`verify` runs gates G1 to G4 on a package folder and prints the report. It writes nothing: no registry row, no blob, no ledger. It still
needs a sandbox, because G2 to G4 run the package's code there; package code never runs in this process.

`submit` registers the package as a new version and runs the same gates, recording every result. The exit code of both is 0 when the
package passed every gate that could run (`verified_basic` or `verified`) and 1 when it was rejected.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from toolforge import cli_run
from toolforge.config import load_settings
from toolforge.errors import ValidationError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry
from toolforge.services.forge import Forge
from toolforge.verify.g4_fuzz import DEFAULT_INPUTS, MAX_INPUTS
from toolforge.verify.pipeline import Pipeline, VerificationReport

DEFAULT_SUBMITTER = "operator"


def _inputs(value: int) -> int:
    if not 1 <= value <= MAX_INPUTS:
        raise ValidationError(
            f"--fuzz-inputs must be from 1 to {MAX_INPUTS}",
            code="bad_option",
            hint=f"the default is {DEFAULT_INPUTS}",
        )
    return value


def _show(report: VerificationReport, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=True))
    else:
        print(report.text())
    return 0 if report.ok else 1


def _shutdown(sandbox: object) -> None:
    stop: Callable[[], object] | None = getattr(sandbox, "shutdown", None)
    if stop is not None:
        stop()


def _cmd_verify(args: argparse.Namespace) -> int:
    settings = load_settings()
    package = ToolPackage.from_dir(args.dir)
    sandbox = cli_run.make_sandbox_runner(settings)
    try:
        report = Pipeline(settings, sandbox, None, fuzz_inputs=_inputs(args.fuzz_inputs)).run(
            package, DEFAULT_SUBMITTER
        )
    finally:
        _shutdown(sandbox)
    return _show(report, args.json)


def _cmd_submit(args: argparse.Namespace) -> int:
    settings = load_settings()
    package = ToolPackage.from_dir(args.dir)
    sandbox = cli_run.make_sandbox_runner(settings)
    try:
        with Registry.from_settings(settings) as registry:
            forge = Forge(
                settings,
                registry,
                sandbox,
                ledger_factory=cli_run.make_ledger,
                fuzz_inputs=_inputs(args.fuzz_inputs),
            )
            report = forge.submit_package(package, args.by)
    finally:
        _shutdown(sandbox)
    return _show(report, args.json)


def register(subparsers: argparse._SubParsersAction) -> None:
    verify = subparsers.add_parser(
        "verify",
        help="run the verification gates on a package folder without registering it",
        description=(
            "Dry run of gates G1 to G4: nothing is written to the registry or the ledger. The package's code runs only "
            "inside a sandbox (Docker is needed for G2 to G4)."
        ),
    )
    verify.add_argument("dir", help="the folder that contains manifest.json")
    verify.add_argument("--json", action="store_true", help="print the report as JSON")
    verify.add_argument(
        "--fuzz-inputs", type=int, default=DEFAULT_INPUTS, help="inputs for gate G4 (default 30)"
    )
    verify.set_defaults(handler=_cmd_verify)

    submit = subparsers.add_parser(
        "submit",
        help="register a package as a new version and verify it",
        description=(
            "Register the package folder as a new version and run the gates, recording every result in the registry and the "
            "ledger. A version is never changed once registered: change the code, raise the version, submit again."
        ),
    )
    submit.add_argument("dir", help="the folder that contains manifest.json")
    submit.add_argument(
        "--by", default=DEFAULT_SUBMITTER, help="who is submitting (recorded; default: operator)"
    )
    submit.add_argument("--json", action="store_true", help="print the report as JSON")
    submit.add_argument(
        "--fuzz-inputs", type=int, default=DEFAULT_INPUTS, help="inputs for gate G4 (default 30)"
    )
    submit.set_defaults(handler=_cmd_submit)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
