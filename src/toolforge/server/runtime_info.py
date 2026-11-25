"""The MCP server's own runtime information: version, capabilities, the image details that run each tool.

Returned to clients as a transparent json-schema compatible object, and used by the CLI's `toolforge doctor`.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pkg_resources

if TYPE_CHECKING:
    from toolforge.config import Settings


@dataclass(frozen=True)
class RuntimeImageInfo:
    """A runtime image: a Docker container with software installed that can run tools."""

    # The exact digest of the runtime image, prefixed with its repository (e.g. `toolforge/runtime:latest@sha256:...`).
    digest: str

    # The friendly name that appears in logs and in the tool's `runtime_image` field.
    name: str

    # When this image was built (iso8601), for freshness checks.
    built_at: str

    # A list of software packages and their versions included in this image.
    packages: dict[str, str]

    # Whether this image is considered deprecated: newer tools should not target it.
    deprecated: bool = False


class RuntimeInfo:
    """Collects runtime‑image information and other static facts about this ToolForge server."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.images: dict[str, RuntimeImageInfo] = {}
        self._load_images()

    def _load_images(self) -> None:
        """Read the images registered under `self.settings.images_dir` (default `$TOOLFORGE_HOME/images/`)."""
        # Default location: $TOOLFORGE_HOME/images/, or the configurable setting.
        images_dir = self.settings.images_dir
        if not images_dir or not os.path.isdir(images_dir):
            # No images have been built yet.
            return

        for manifest_file in Path(images_dir).glob("*/manifest.json"):
            try:
                with open(manifest_file) as f:
                    data = json.load(f)
                image = RuntimeImageInfo(
                    digest=data["digest"],
                    name=data["name"],
                    built_at=data["built_at"],
                    packages=data.get("packages", {}),
                    deprecated=data.get("deprecated", False),
                )
                self.images[image.name] = image
            except (OSError, json.JSONDecodeError, KeyError):
                continue  # skip malformed manifests silently

    def to_dict(self) -> dict[str, object]:
        """Everything a client may want to know about the server, as plain JSON."""

        from toolforge.sandbox.minilocker_runner import MiniLockerSandbox

        # Which sandbox backends are usable (some may be unavailable on this system).
        backends: dict[str, bool] = {}
        backends["fake"] = True  # always available
        try:
            # The import may already have happened; we just need to know whether the backend can be instantiated.
            sandbox = MiniLockerSandbox()
            backends["minilocker"] = True
            sandbox.close()
        except Exception:
            backends["minilocker"] = False

        version = "unknown"
        try:
            version = pkg_resources.get_distribution("toolforge").version
        except Exception:
            pass

        return {
            "server": {
                "version": version,
                "python": {
                    "implementation": platform.python_implementation(),
                    "version": platform.python_version(),
                },
                "platform": {
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "release": platform.release(),
                },
                "settings": {
                    "home": str(self.settings.home),
                    "images_dir": str(self.settings.images_dir),
                    "sandbox": self.settings.sandbox,
                    "sandbox_timeout_s": self.settings.sandbox_timeout_s,
                    "log_level": self.settings.log_level,
                    "max_blob_bytes": self.settings.max_blob_bytes,
                    "max_input_bytes": self.settings.max_input_bytes,
                    "max_output_bytes": self.settings.max_output_bytes,
                },
            },
            "sandboxes": {
                "available": backends,
                "selected": self.settings.sandbox,
            },
            "images": {
                name: {
                    "digest": image.digest,
                    "built_at": image.built_at,
                    "packages": image.packages,
                    "deprecated": image.deprecated,
                }
                for name, image in self.images.items()
            },
            "now": datetime.utcnow().isoformat() + "Z",
        }


def runtime_info(settings: Settings) -> dict[str, object]:
    """Helper for the single‑endpoint case."""
    return RuntimeInfo(settings).to_dict()
