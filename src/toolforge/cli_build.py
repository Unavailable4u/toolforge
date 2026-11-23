"""`toolforge build "<goal>"`: say what a tool must do in words; the builder model writes it and the gates verify it.

The same code as the `forge_build` MCP tool (`services/builder.py`). It needs the builder model (`TOOLFORGE_LLM_*`) for anything but
reuse, and Docker for the gates that run the package. The package's code runs only inside a sandbox, never in this process.

Progress notes go to stderr, the result to stdout. The exit code is 0 when the build ended with a tool the pipeline did not reject (or
reused an active one) and 1 when it did not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from toolforge import cli_run
from toolforge.config import load_settings
from toolforge.errors import ValidationError
from toolforge.llm.client import client_from_settings
from toolforge.registry import Registry
from toolforge.server.tools import confidence, runtime_facts
from toolforge.services.builder import MAX_SAMPLE_BYTES, Builder
from toolforge.services.forge import Forge


def _bad_option(message: str, hint: str) -> ValidationError:
    return ValidationError(message, code="bad_option", hint=hint)


def read_examples(path: str | None) -> list[Any] | None:
    """The `--examples` file: a JSON list of `{"input": {...}, "output": ...}`. The builder checks each example's shape."""
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _bad_option(
            f"--examples {path!r} could not be read as JSON ({type(exc).__name__})",
            'the file holds a list such as [{"input": {"text": "a b"}, "output": {"words": 2}}]',
        ) from None
    if not isinstance(value, list):
        raise _bad_option(f"--examples {path!r} is not a JSON list", "wrap the examples in [ ... ]")
    return value


def read_samples(paths: list[str]) -> dict[str, str] | None:
    """`--sample` files as `{file name: text}`. A file that is large is refused here, before it is read whole."""
    if not paths:
        return None
    out: dict[str, str] = {}
    for path in paths:
        target = Path(path)
        try:
            with target.open("rb") as handle:
                raw = handle.read(MAX_SAMPLE_BYTES + 1)
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _bad_option(
                f"--sample {path!r} could not be read as UTF-8 text ({type(exc).__name__})",
                "samples are small text files: the builder needs the shape of the data, not all of it",
            ) from None
        if len(raw) > MAX_SAMPLE_BYTES:
            raise _bad_option(
                f"--sample {path!r} is larger than {MAX_SAMPLE_BYTES // 1024} KB",
                "send the first rows or lines only",
            )
        if target.name in out:
            raise _bad_option(f"two --sample files are both called {target.name!r}", "rename one of them")
        out[target.name] = text
    return out


def _cmd_build(args: argparse.Namespace) -> int:
    settings = load_settings()
    examples = read_examples(args.examples)
    samples = read_samples(args.sample)

    def note(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    sandbox = cli_run.make_sandbox_runner(settings)
    try:
        with Registry.from_settings(settings) as registry:
            forge = Forge(settings, registry, sandbox, ledger_factory=cli_run.make_ledger)
            builder = Builder(
                settings,
                registry,
                forge,
                client=client_from_settings(settings, "builder"),
                ledger_factory=cli_run.make_ledger,
                runtime_facts=lambda: runtime_facts(settings),
                confidence=confidence,
            )
            result = builder.build(
                args.goal,
                examples=examples,
                sample_files=samples,
                constraints=args.constraints,
                on_progress=note,
            )
    finally:
        stop = getattr(sandbox, "shutdown", None)
        if stop is not None:
            stop()
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=True))
    else:
        print(result.text())
    return 0 if result.ok else 1


def register(subparsers: argparse._SubParsersAction) -> None:
    build = subparsers.add_parser(
        "build",
        help="describe a tool in words; the builder model writes it and the gates verify it",
        description=(
            "Search the active tools for one that already fits (nothing is built then). Otherwise ask the builder model "
            "(TOOLFORGE_LLM_*) for a package, run it through the same gates as any submission, and feed a failure back to the model "
            "until a package passes or a limit is reached (TOOLFORGE_BUILDER_MAX_ROUNDS, TOOLFORGE_BUILDER_TOKEN_BUDGET). A built tool "
            "is not active: the result says what the operator does next, usually `toolforge promote`."
        ),
    )
    build.add_argument("goal", help="what the tool must do, in plain words (at most 2000 characters)")
    build.add_argument("--constraints", help="anything else the tool must respect (at most 1000 characters)")
    build.add_argument(
        "--examples",
        metavar="FILE",
        help='a JSON file with a list of {"input": {...}, "output": ...}: calls and the results you expect (at most 5)',
    )
    build.add_argument(
        "--sample",
        action="append",
        default=[],
        metavar="FILE",
        help="a small sample of the data the tool will handle; repeat for more files (20 KB in all). It is read by the tool as inputs/<file name>",
    )
    build.add_argument("--json", action="store_true", help="print the result as JSON")
    build.add_argument("--quiet", action="store_true", help="do not print progress notes to stderr")
    build.set_defaults(handler=_cmd_build)


__all__ = ["read_examples", "read_samples", "register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
