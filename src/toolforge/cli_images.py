"""`toolforge images …` – build, list and inspect runtime images and adapter kits (P14)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from toolforge.config import load_settings
from toolforge.errors import ToolForgeError
from toolforge.server.runtime_info import RuntimeInfo


def _cmd_images_list(args: argparse.Namespace) -> int:
    """Show every runtime image the server knows about."""
    settings = load_settings()
    info = RuntimeInfo(settings)
    if not info.images:
        print("No runtime images have been built.", file=sys.stderr)
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    name: {
                        "digest": img.digest,
                        "built_at": img.built_at,
                        "packages": img.packages,
                        "deprecated": img.deprecated,
                    }
                    for name, img in info.images.items()
                },
                indent=2,
            )
        )
        return 0

    now = datetime.utcnow()
    for name, image in sorted(info.images.items()):
        try:
            built = datetime.fromisoformat(image.built_at.replace("Z", "+00:00"))
            age = (now - built).days
        except Exception:
            age = "?"

        packages = list(image.packages.items())
        print(f"  {name}")
        print(f"    digest     {image.digest[:64]}…")
        print(f"    built      {image.built_at} ({age} days ago)")
        if image.deprecated:
            print("    DEPRECATED")
        if packages:
            print(f"    packages   {len(packages)}")
            for pkg, ver in packages[:3]:
                print(f"      {pkg} = {ver}")
            if len(packages) > 3:
                print(f"      … and {len(packages) - 3} more")
        print()
    return 0


def _cmd_images_build(args: argparse.Namespace) -> int:
    """Build a runtime image by running a recipe."""
    recipe = Path(args.recipe)
    if not recipe.is_dir():
        raise ToolForgeError(
            f"{recipe} is not a directory (a recipe is a directory).",
            code="not_a_directory",
        )
    if not (recipe / "Dockerfile").exists():
        raise ToolForgeError(
            f"{recipe}/Dockerfile does not exist.",
            code="no_dockerfile",
        )

    image_name = args.name or recipe.name
    image_tag = args.tag or f"toolforge/runtime:{image_name}"
    print(f"Building {image_name} as {image_tag} …")

    # Build the Docker image.
    try:
        result = subprocess.run(
            ["docker", "build", "--tag", image_tag, str(recipe)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"docker build failed:\n{result.stderr}", file=sys.stderr)
            return 1
    except FileNotFoundError:
        raise ToolForgeError(
            "Docker is not installed or not in PATH.",
            code="docker_unavailable",
            hint="Install Docker and make sure the `docker` command works.",
        )

    # Retrieve the image digest.
    try:
        inspect = subprocess.run(
            ["docker", "inspect", "--format={{.RepoDigests}}", image_tag],
            capture_output=True,
            text=True,
        )
        if inspect.returncode != 0:
            print(f"docker inspect failed:\n{inspect.stderr}", file=sys.stderr)
            return 1
    except Exception as e:
        raise ToolForgeError(f"Cannot inspect the built image: {e}", code="image_inspect_failed")

    digest = inspect.stdout.strip()
    if not digest or digest == "[]":
        raise ToolForgeError("The built image has no digest.", code="no_image_digest")

    # Extract the digest (strip the brackets and quotes).
    digest = digest[2:-2]  # e.g., '[docker.io/toolforge/runtime:…@sha256:…]' -> 'docker.io/…'
    print(f"  digest {digest}")

    # TODO: run the recipe's test step if a `test.sh` exists.

    # Write the manifest to the images directory.
    settings = load_settings()
    images_dir = Path(settings.images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = images_dir / image_name / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # TODO: read the actual installed packages from the image (run `dpkg -l` etc.)
    packages = {}
    if (recipe / "packages.json").exists():
        try:
            with open(recipe / "packages.json") as f:
                packages = json.load(f)
        except Exception:
            print("Warning: could not read packages.json", file=sys.stderr)

    manifest = {
        "digest": digest,
        "name": image_name,
        "built_at": datetime.utcnow().isoformat() + "Z",
        "packages": packages,
        "deprecated": False,
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written to {manifest_path}")
    if args.push:
        try:
            subprocess.run(["docker", "push", image_tag], check=True)
            print(f"Pushed {image_tag} to registry.")
        except subprocess.CalledProcessError as e:
            raise ToolForgeError(f"Failed to push: {e}", code="push_failed")
    return 0


def _cmd_images_inspect(args: argparse.Namespace) -> int:
    """Display the manifest of a runtime image."""
    settings = load_settings()
    info = RuntimeInfo(settings)
    if args.name not in info.images:
        raise ToolForgeError(f"No runtime image named {args.name!r}.", code="image_not_found")

    image = info.images[args.name]
    if args.json:
        print(
            json.dumps(
                {
                    "name": image.name,
                    "digest": image.digest,
                    "built_at": image.built_at,
                    "packages": image.packages,
                    "deprecated": image.deprecated,
                },
                indent=2,
            )
        )
        return 0

    print(f"Image: {image.name}")
    print(f"Digest: {image.digest}")
    print(f"Built:  {image.built_at}")
    print(f"Deprecated: {'Yes' if image.deprecated else 'No'}")
    if image.packages:
        print("\nPackages:")
        for pkg, ver in sorted(image.packages.items()):
            print(f"  {pkg} = {ver}")
    return 0


def _cmd_images_deprecate(args: argparse.Namespace) -> int:
    """Mark (or un‑mark) a runtime image as deprecated."""
    settings = load_settings()
    images_dir = Path(settings.images_dir)
    manifest_path = images_dir / args.name / "manifest.json"
    if not manifest_path.exists():
        raise ToolForgeError(f"No runtime image named {args.name!r}.", code="image_not_found")

    with open(manifest_path) as f:
        manifest = json.load(f)

    manifest["deprecated"] = not args.undo
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    action = "undeprecated" if args.undo else "deprecated"
    print(f"Image {args.name!r} {action}.")
    return 0


def _cmd_images_toolpacks(args: argparse.Namespace) -> int:
    """Show every adapter kit that ships with ToolForge."""
    toolpacks_dir = Path(__file__).parent.parent / "examples" / "toolpacks"
    if not toolpacks_dir.exists():
        print("No bundled toolpacks.", file=sys.stderr)
        return 0

    packs = []
    for pack_dir in sorted(toolpacks_dir.iterdir()):
        if not pack_dir.is_dir():
            continue
        manifest_path = pack_dir / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
            except Exception:
                manifest = {"name": pack_dir.name, "description": "(manifest broken)"}
        else:
            manifest = {"name": pack_dir.name, "description": "(no manifest)"}
        packs.append(manifest)

    if args.json:
        print(json.dumps(packs, indent=2))
        return 0

    if not packs:
        print("No adapter kits found.")
        return 0

    print("Bundled adapter kits:")
    for manifest in packs:
        print(f"  {manifest.get('name', '?')}")
        desc = manifest.get("description")
        if desc:
            print(f"    {desc}")
        runtime = manifest.get("runtime_image")
        if runtime:
            print(f"    requires runtime: {runtime}")
        print()
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the `images` subcommand and its subcommands."""
    # Main images command group
    images = subparsers.add_parser(
        "images",
        help="manage runtime images and adapter kits",
        description=(
            "Build, list and inspect runtime images (Docker containers with pre‑installed software) "
            "and view the bundled adapter kits that ship with ToolForge."
        ),
    )
    images_subparsers = images.add_subparsers(dest="images_command", metavar="<subcommand>")

    # images list
    list_parser = images_subparsers.add_parser(
        "list",
        help="list the runtime images that can run tools",
        description="Show every runtime image the server knows about.",
    )
    list_parser.add_argument("--json", action="store_true", help="output as JSON")
    list_parser.set_defaults(handler=_cmd_images_list)

    # images build
    build_parser = images_subparsers.add_parser(
        "build",
        help="build a runtime image from a recipe",
        description="Build a runtime image by running a recipe (a directory with a Dockerfile).",
    )
    build_parser.add_argument("recipe", help="path to the recipe directory")
    build_parser.add_argument(
        "--name",
        help="name for the built image (default: the recipe's directory name)",
    )
    build_parser.add_argument(
        "--tag",
        help="Docker tag to apply (default: `toolforge/runtime:{name}`)",
    )
    build_parser.add_argument("--push", action="store_true", help="push the built image to the registry")
    build_parser.set_defaults(handler=_cmd_images_build)

    # images inspect
    inspect_parser = images_subparsers.add_parser(
        "inspect",
        help="show details of one runtime image",
        description="Display the manifest of a runtime image.",
    )
    inspect_parser.add_argument("name", help="image name")
    inspect_parser.add_argument("--json", action="store_true", help="output as JSON")
    inspect_parser.set_defaults(handler=_cmd_images_inspect)

    # images deprecate
    deprecate_parser = images_subparsers.add_parser(
        "deprecate",
        help="mark a runtime image as deprecated",
        description="Mark (or un‑mark) a runtime image as deprecated.",
    )
    deprecate_parser.add_argument("name", help="image name")
    deprecate_parser.add_argument("--undo", action="store_true", help="remove the deprecated flag")
    deprecate_parser.set_defaults(handler=_cmd_images_deprecate)

    # images toolpacks
    toolpacks_parser = images_subparsers.add_parser(
        "toolpacks",
        help="list the bundled adapter kits",
        description="Show every adapter kit that ships with ToolForge.",
    )
    toolpacks_parser.add_argument("--json", action="store_true", help="output as JSON")
    toolpacks_parser.set_defaults(handler=_cmd_images_toolpacks)


__all__ = ["register"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
