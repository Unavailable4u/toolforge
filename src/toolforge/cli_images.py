"""`toolforge images …` – build runtime images (P14)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from toolforge.config import load_settings
from toolforge.errors import ToolForgeError


def _find_default_context() -> Path:
    """Find the images/runtime directory."""
    cwd_path = Path.cwd() / "images" / "runtime"
    if cwd_path.is_dir():
        return cwd_path
    repo_root = Path(__file__).resolve().parent.parent.parent
    repo_path = repo_root / "images" / "runtime"
    if repo_path.is_dir():
        return repo_path
    return cwd_path


def _cmd_images_build(args: argparse.Namespace) -> int:
    """Build a runtime image with docker build."""
    settings = load_settings()
    tag = args.tag or settings.runtime_image or "toolforge-runtime:py312"

    if args.context:
        context = Path(args.context)
    else:
        context = _find_default_context()

    if not context.is_dir():
        raise ToolForgeError(
            f"Context directory {context} does not exist.",
            code="no_context",
        )
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        raise ToolForgeError(
            f"{dockerfile} does not exist.",
            code="no_dockerfile",
        )

    cmd = ["docker", "build", "-t", tag, str(context)]
    if getattr(args, "no_cache", False):
        cmd.append("--no-cache")

    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except FileNotFoundError:
        raise ToolForgeError(
            "Docker is not installed or not in PATH.",
            code="docker_unavailable",
            hint="Install Docker and make sure the `docker` command works.",
        )


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the `images` subcommand."""
    images = subparsers.add_parser(
        "images",
        help="manage runtime images",
        description="Build runtime images for ToolForge tools.",
    )
    images_subparsers = images.add_subparsers(dest="images_command", metavar="<subcommand>")

    build_parser = images_subparsers.add_parser(
        "build",
        help="build the runtime image (wrapper over docker build)",
        description="Build the runtime Docker image.",
    )
    build_parser.add_argument(
        "context",
        nargs="?",
        default=None,
        help="path to the Dockerfile context directory (default: images/runtime)",
    )
    build_parser.add_argument(
        "--tag",
        "-t",
        default=None,
        help="Docker tag to apply (default: toolforge-runtime:py312)",
    )
    build_parser.add_argument(
        "--no-cache",
        action="store_true",
        help="do not use cache when building the image",
    )
    build_parser.set_defaults(handler=_cmd_images_build)


__all__ = ["register"]
