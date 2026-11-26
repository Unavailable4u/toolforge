"""`toolforge images build` (P14a): build the runtime image that tool sandboxes run in.

A thin wrapper over `docker build`. It builds `images/runtime/Dockerfile` (a checkout of this repository carries the folder; a copy of
the package without it is told so) and tags the result `toolforge-runtime:py312`, the default `TOOLFORGE_RUNTIME_IMAGE`. Docker is
started with an argument list, never a shell string, and the tag is checked first, so nothing a person types can become an option.

The image is local only: it is not on any registry, so a sandbox cannot pull it and `toolforge doctor` says to build it. Building is
the one step that needs the network (the base image and the pinned wheels); running tools never does.

Proven by `tests/test_cli_images.py` (the command line, the refusals, the exit code and the digest line, with a stand-in for Docker)
and `tests/docker/test_runtime_image.py` (a real build).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from toolforge.config import RUNTIME_IMAGE, load_settings
from toolforge.errors import ToolForgeError

# `name[:tag]` with an optional registry path; a leading dash or a space never matches, so a tag cannot be read as an option.
TAG = re.compile(r"[a-z0-9][a-z0-9._/-]{0,100}(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?")

Run = Callable[[list[str]], int]
Inspect = Callable[[str], "str | None"]


def default_context() -> Path:
    """`images/runtime/` of the checkout this package was installed from (an editable install, or running from the repository)."""
    return Path(__file__).resolve().parents[2] / "images" / "runtime"


def build_command(tag: str, context: Path, *, no_cache: bool = False) -> list[str]:
    """The `docker build` argument list. `tag` must already have been checked with `TAG`."""
    command = ["docker", "build", "--tag", tag, "--file", str(context / "Dockerfile")]
    if no_cache:
        command.append("--no-cache")
    command.append(str(context))
    return command


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


def _inspect(tag: str) -> str | None:
    """The image id Docker reports for `tag` (`sha256:...`), or None when it cannot be read."""
    try:
        done = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out.startswith("sha256:") else None


def build_image(
    tag: str = RUNTIME_IMAGE,
    *,
    context: Path | None = None,
    no_cache: bool = False,
    configured: str | None = None,
    run: Run = _run,
    inspect: Inspect = _inspect,
    which: Callable[[str], str | None] = shutil.which,
    out=None,
) -> int:
    """Build the runtime image and print what was built. Returns Docker's exit code (0 on success)."""
    out = out if out is not None else sys.stdout
    if not TAG.fullmatch(tag):
        raise ToolForgeError(
            f"{tag[:60]!r} is not a usable image tag",
            code="bad_image_tag",
            hint="use lower-case letters, digits, '.', '_', '-' and '/', then an optional :tag, for example toolforge-runtime:py312",
        )
    folder = context if context is not None else default_context()
    if not (folder / "Dockerfile").is_file():
        raise ToolForgeError(
            f"there is no Dockerfile in {folder}",
            code="no_dockerfile",
            hint="run this from a checkout of the repository (it holds images/runtime/), or name the folder with --context",
        )
    if which("docker") is None:
        raise ToolForgeError(
            "the docker command was not found",
            code="no_docker_cli",
            hint="install Docker (on Windows, Docker Desktop with WSL2 integration) and run `toolforge doctor`",
        )
    code = run(build_command(tag, folder, no_cache=no_cache))
    if code != 0:
        print(
            f"docker build failed (exit code {code}); the lines above say why. Building needs the network once: "
            "the base image and the pinned wheels are downloaded.",
            file=sys.stderr,
        )
        return code
    image_id = inspect(tag)
    print(f"built {tag} ({image_id[:19] if image_id else 'digest not read'})", file=out)
    if configured is not None and configured != tag:
        print(
            f"note: TOOLFORGE_RUNTIME_IMAGE is {configured}, so sandboxes do not use this image yet; "
            f"set TOOLFORGE_RUNTIME_IMAGE={tag} to use it",
            file=out,
        )
    print("next: run `toolforge doctor` to check it", file=out)
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    return build_image(
        args.tag,
        context=Path(args.context) if args.context else None,
        no_cache=args.no_cache,
        configured=load_settings().runtime_image,
    )


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "images",
        help="build the runtime image tool sandboxes run in",
        description="The runtime image: python:3.12-slim plus a fixed set of pinned libraries (docs/runtime-image.md).",
    )
    sub = parser.add_subparsers(dest="images_command", metavar="<action>", required=True)
    build = sub.add_parser(
        "build",
        help=f"build {RUNTIME_IMAGE} from images/runtime/ (needs the network once)",
        description="A thin wrapper over `docker build`. The image stays on this machine; nothing is pushed.",
    )
    build.add_argument("--tag", default=RUNTIME_IMAGE, help=f"image name and tag (default {RUNTIME_IMAGE})")
    build.add_argument(
        "--context", help="folder with the Dockerfile (default: images/runtime/ of this checkout)"
    )
    build.add_argument("--no-cache", action="store_true", help="rebuild every layer")
    build.set_defaults(handler=_cmd_build)


__all__ = ["TAG", "build_command", "build_image", "default_context", "register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
