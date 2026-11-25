"""Adapter‑kit specific CLI commands (the `toolforge images` group)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from toolforge.config import create_settings
from toolforge.errors import ToolForgeError


@click.group(name="images", help="Manage runtime images and adapter kits.")
def cli() -> None:
    pass


def _runtime_image_list(json_output: bool) -> None:
    """Shared logic for listing runtime images."""
    settings = create_settings()
    from toolforge.server.runtime_info import RuntimeInfo

    info = RuntimeInfo(settings)
    if not info.images:
        click.echo("No runtime images have been built.", err=True)
        return

    if json_output:
        click.echo(
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
        return

    from datetime import datetime

    now = datetime.utcnow()
    for name, image in sorted(info.images.items()):
        try:
            built = datetime.fromisoformat(image.built_at.replace("Z", "+00:00"))
            age = (now - built).days
        except Exception:
            age = "?"

        packages = list(image.packages.items())
        click.echo(f"  {name}")
        click.echo(f"    digest     {image.digest[:64]}…")
        click.echo(f"    built      {image.built_at} ({age} days ago)")
        if image.deprecated:
            click.echo("    DEPRECATED")
        if packages:
            click.echo(f"    packages   {len(packages)}")
            for pkg, ver in packages[:3]:
                click.echo(f"      {pkg} = {ver}")
            if len(packages) > 3:
                click.echo(f"      … and {len(packages) - 3} more")
        click.echo()


@cli.command(name="list", help="List the runtime images that can run tools.")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def list_images(json_output: bool) -> None:
    """Show every runtime image the server knows about."""
    _runtime_image_list(json_output)


@cli.command(help="Build a runtime image from a recipe.")
@click.argument("recipe", type=click.Path(exists=True, dir_okay=True, path_type=Path))
@click.option("--name", help="Name for the built image (default: the recipe's directory name).")
@click.option("--tag", help="Docker tag to apply (default: `toolforge/runtime:{name}`).")
@click.option("--push", is_flag=True, help="Push the built image to the registry.")
def build(recipe: Path, name: str | None, tag: str | None, push: bool) -> None:
    """Build a runtime image by running a recipe."""
    if not recipe.is_dir():
        raise click.BadParameter(f"{recipe} is not a directory (a recipe is a directory).")
    if not (recipe / "Dockerfile").exists():
        raise click.BadParameter(f"{recipe}/Dockerfile does not exist.")

    image_name = name or recipe.name
    image_tag = tag or f"toolforge/runtime:{image_name}"
    click.echo(f"Building {image_name} as {image_tag} …")

    # Build the Docker image.
    try:
        result = subprocess.run(
            ["docker", "build", "--tag", image_tag, str(recipe)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.echo(f"docker build failed:\n{result.stderr}", err=True)
            sys.exit(1)
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
            click.echo(f"docker inspect failed:\n{inspect.stderr}", err=True)
            sys.exit(1)
    except Exception as e:
        raise ToolForgeError(f"Cannot inspect the built image: {e}", code="image_inspect_failed")

    digest = inspect.stdout.strip()
    if not digest or digest == "[]":
        raise ToolForgeError("The built image has no digest.", code="no_image_digest")

    # Extract the digest (strip the brackets and quotes).
    digest = digest[2:-2]  # e.g., '[docker.io/toolforge/runtime:…@sha256:…]' -> 'docker.io/…'
    click.echo(f"  digest {digest}")

    # TODO: run the recipe's test step if a `test.sh` exists.

    # Write the manifest to the images directory.
    settings = create_settings()
    images_dir = Path(settings.images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = images_dir / image_name / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    from datetime import datetime

    # TODO: read the actual installed packages from the image (run `dpkg -l` etc.)
    packages = {}
    if (recipe / "packages.json").exists():
        try:
            with open(recipe / "packages.json") as f:
                packages = json.load(f)
        except Exception:
            click.echo("Warning: could not read packages.json", err=True)

    manifest = {
        "digest": digest,
        "name": image_name,
        "built_at": datetime.utcnow().isoformat() + "Z",
        "packages": packages,
        "deprecated": False,
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    click.echo(f"Manifest written to {manifest_path}")
    if push:
        try:
            subprocess.run(["docker", "push", image_tag], check=True)
            click.echo(f"Pushed {image_tag} to registry.")
        except subprocess.CalledProcessError as e:
            raise ToolForgeError(f"Failed to push: {e}", code="push_failed")


@cli.command(help="Show details of one runtime image.")
@click.argument("name")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def inspect(name: str, json_output: bool) -> None:
    """Display the manifest of a runtime image."""
    settings = create_settings()
    from toolforge.server.runtime_info import RuntimeInfo

    info = RuntimeInfo(settings)
    if name not in info.images:
        raise click.BadParameter(f"No runtime image named {name!r}.")

    image = info.images[name]
    if json_output:
        click.echo(
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
        return

    click.echo(f"Image: {image.name}")
    click.echo(f"Digest: {image.digest}")
    click.echo(f"Built:  {image.built_at}")
    click.echo(f"Deprecated: {'Yes' if image.deprecated else 'No'}")
    if image.packages:
        click.echo("\nPackages:")
        for pkg, ver in sorted(image.packages.items()):
            click.echo(f"  {pkg} = {ver}")


@cli.command(help="Mark a runtime image as deprecated.")
@click.argument("name")
@click.option("--undo", is_flag=True, help="Remove the deprecated flag.")
def deprecate(name: str, undo: bool) -> None:
    """Mark (or un‑mark) a runtime image as deprecated."""
    settings = create_settings()
    images_dir = Path(settings.images_dir)
    manifest_path = images_dir / name / "manifest.json"
    if not manifest_path.exists():
        raise click.BadParameter(f"No runtime image named {name!r}.")

    with open(manifest_path) as f:
        manifest = json.load(f)

    manifest["deprecated"] = not undo
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    action = "undeprecated" if undo else "deprecated"
    click.echo(f"Image {name!r} {action}.")


@cli.command(name="toolpacks", help="List the bundled adapter kits.")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def list_toolpacks(json_output: bool) -> None:
    """Show every adapter kit that ships with ToolForge."""
    toolpacks_dir = Path(__file__).parent.parent.parent / "examples" / "toolpacks"
    if not toolpacks_dir.exists():
        click.echo("No bundled toolpacks.", err=True)
        return

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

    if json_output:
        click.echo(json.dumps(packs, indent=2))
        return

    if not packs:
        click.echo("No adapter kits found.")
        return

    click.echo("Bundled adapter kits:")
    for manifest in packs:
        click.echo(f"  {manifest.get('name', '?')}")
        desc = manifest.get("description")
        if desc:
            click.echo(f"    {desc}")
        runtime = manifest.get("runtime_image")
        if runtime:
            click.echo(f"    requires runtime: {runtime}")
        click.echo()
