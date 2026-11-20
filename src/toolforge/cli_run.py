"""`toolforge run`: call one tool, in a sandbox, and print the `CallResult` as JSON.

Two forms:

    toolforge run <name or name@version> --args '{...}' [--file path ...]
    toolforge run --package <dir> --args '{...}' --dev-unverified [--file path ...]

The first runs a registered, active version. The second builds a package straight from a folder with no registry entry and no gate
ever having run; it prints a warning, records nothing in the registry and still runs only in the sandbox. Neither form runs tool
code in this process. Everything printed comes out of `json.dumps` with ASCII escapes, so text produced by a tool cannot move the
operator's cursor. The exit code is 0 when the call succeeded and 1 when it failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from toolforge.config import Settings, load_settings
from toolforge.errors import ValidationError
from toolforge.models.package import ToolPackage
from toolforge.registry import Registry
from toolforge.sandbox.base import SandboxRunner
from toolforge.services.runner import MAX_FILES, MAX_FILES_BYTES, LedgerLike, ToolRunner

UNVERIFIED_WARNING = (
    "warning: --dev-unverified runs a package that no gate has checked. It still runs only inside a sandbox, "
    "and nothing about this call is recorded in the registry."
)


def make_sandbox_runner(settings: Settings) -> SandboxRunner:
    """The real runner (hardened Docker containers). Tests replace this to avoid Docker."""
    from toolforge.sandbox import MiniLockerRunner

    return MiniLockerRunner(settings=settings)


def make_ledger(path: str | None, task_id: str) -> LedgerLike:
    """The real hash-chained ledger. Tests replace this to avoid MiniLocker."""
    from toolforge.ledger import ToolforgeLedger

    return ToolforgeLedger(path, task_id)


def _bad(message: str, hint: str) -> ValidationError:
    return ValidationError(message, code="bad_run_request", hint=hint)


def _parse_args(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        raise _bad(
            "--args is not valid JSON", 'pass a JSON object, for example --args \'{"text": "a b c"}\''
        ) from None
    if not isinstance(value, dict):
        raise _bad(
            "--args must be a JSON object", 'pass a JSON object, for example --args \'{"text": "a b c"}\''
        )
    return value


def _read_files(paths: list[str]) -> dict[str, bytes]:
    """Each local file becomes `inputs/<its name>`. Reads at most one byte more than the limit so a huge file is not loaded."""
    if len(paths) > MAX_FILES:
        raise _bad(f"{len(paths)} files were given; at most {MAX_FILES} are allowed", "attach fewer files")
    files: dict[str, bytes] = {}
    total = 0
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise _bad(f"--file {raw!r} is not a file", "give the path of a file that exists")
        key = f"inputs/{path.name}"
        if key in files:
            raise _bad(f"two --file arguments are both called {path.name!r}", "rename one of them")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILES_BYTES + 1)
        total += len(data)
        if total > MAX_FILES_BYTES:
            raise _bad(f"the files are larger than {MAX_FILES_BYTES // 1024} KB together", "attach less data")
        files[key] = data
    return files


def _cmd_run(args: argparse.Namespace) -> int:
    if (args.name is None) == (args.package is None):
        raise _bad(
            "name a registered tool or give --package, not both and not neither",
            "toolforge run <name> --args '{...}'   or   toolforge run --package <dir> --args '{...}' --dev-unverified",
        )
    if args.package is not None and not args.dev_unverified:
        raise _bad(
            "--package runs code that no gate has checked, so it needs --dev-unverified",
            "add --dev-unverified, or submit the tool and run it by name",
        )
    if args.name is not None and args.dev_unverified:
        raise _bad("--dev-unverified only applies to --package", "drop --dev-unverified")
    call_args = _parse_args(args.args)
    files = _read_files(args.file or [])
    settings = load_settings()

    sandbox = make_sandbox_runner(settings)
    try:
        if args.package is not None:
            package = ToolPackage.from_dir(args.package)
            print(UNVERIFIED_WARNING, file=sys.stderr)
            tool_runner = ToolRunner(sandbox, None, settings, make_ledger)
            result = tool_runner.run_unverified(package, call_args, files=files)
        else:
            with Registry.from_settings(settings) as registry:
                tool_runner = ToolRunner(sandbox, registry, settings, make_ledger)
                result = tool_runner.run(args.name, call_args, files=files)
    finally:
        shutdown: Callable[[], object] | None = getattr(sandbox, "shutdown", None)
        if shutdown is not None:
            shutdown()
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=True))
    return 0 if result.ok else 1


def register(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser(
        "run",
        help="call a tool in a sandbox and print the result",
        description=(
            "Call a registered tool by name (its active version) or name@version, or, for development, a package folder "
            "with --package and --dev-unverified. The call always runs inside a sandbox; the result is printed as JSON."
        ),
    )
    run.add_argument("name", nargs="?", help="a tool name or name@version")
    run.add_argument("--args", default="{}", help='the arguments as a JSON object (default: "{}")')
    run.add_argument(
        "--file",
        action="append",
        metavar="PATH",
        help="a local file to give the tool; it appears as inputs/<file name> (repeatable)",
    )
    run.add_argument("--package", metavar="DIR", help="a package folder to run without registering it")
    run.add_argument(
        "--dev-unverified",
        action="store_true",
        help="required with --package: you accept that no gate has checked this code",
    )
    run.set_defaults(handler=_cmd_run)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
