"""Gate G2, build: turn the package into the one sealed blob every later gate and every call runs from.

* No `requirements`: nothing is installed and no sandbox opens; the blob is the package archive itself (`ToolPackage.to_tar`).
* With `requirements`: one sandbox with network access to `settings.build_allow` only runs `pip install --only-binary=:all: --no-deps`
  into `/workspace/pkg/.deps` (wheels only, so no `setup.py` of a dependency ever runs), then the workspace is frozen. The sandbox
  closes before the gate returns; nothing installed here is imported, only unpacked.

Either way the blob is checked the way a call will read it (`repack_blob`: plain files, safe names, size caps, the entrypoint file is
present), stored in the registry's blob store and attached to the version. A version that already has a blob (a resumed run) reuses
it: the content is the same by `code_sha`, and a version's blob never changes.

Deviation from the guide's pip command, recorded in `docs/guide/DEVIATIONS.md`: `--no-compile` and `--no-cache-dir` are added, so the
blob does not hold timestamped `.pyc` files and nothing fills the sandbox's `/tmp`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from toolforge import textsafety
from toolforge.errors import ToolNotRunnable
from toolforge.services.runner import repack_blob
from toolforge.verify import sandboxrun
from toolforge.verify.pipeline import GateContext, GateResult, clip, failed, passed

BUILD_TIMEOUT_S = 240
PIP_OUTPUT_CHARS = 1500
PIP_MAX_OUT = 256 * 1024
REQUIREMENTS_PATH = "/workspace/requirements.txt"
DEPS_DIR = "/workspace/pkg/.deps"
PIP_ARGV = [
    "python",
    "-m",
    "pip",
    "install",
    "--no-input",
    "--disable-pip-version-check",
    "--no-deps",
    "--only-binary=:all:",
    "--no-compile",
    "--no-cache-dir",
    "--target",
    DEPS_DIR,
    "-r",
    REQUIREMENTS_PATH,
]
_NO_MATCH = re.compile(r"No matching distribution found for ([A-Za-z0-9][A-Za-z0-9._\-=<>!~\[\],]{0,80})")
_NO_SPACE = re.compile(r"No space left on device|OSError: \[Errno 28\]")
_PIP_ERROR = re.compile(r"ERROR: ([^\n]{1,200})")


def pip_hints(output: str, requirements: list[str]) -> list[str]:
    """What to change, from pip's own output. Only fixed words and the requirement name pip printed are used."""
    hints: list[str] = []
    match = _NO_MATCH.search(output)
    if match:
        hints.append(
            f"pip could not find a wheel for {clip(match.group(1), 60)}; pick a version with a wheel for CPython 3.12 manylinux "
            "(installs use --only-binary, so a source-only release cannot be used)"
        )
    if _NO_SPACE.search(output):
        hints.append(
            "the workspace is 64 MB; prefer libraries already in the runtime image (see forge_runtime_info) or smaller ones"
        )
    if not hints:
        error = _PIP_ERROR.search(output)
        if error:
            hints.append(f"pip reported: {clip(error.group(1), 140)}")
    hints.append("list every dependency in requirements, including indirect ones: installs use --no-deps")
    if requirements:
        hints.append(
            "check the names and pinned versions on https://pypi.org (the sandbox can reach only the allowed index hosts)"
        )
    return hints


class BuildGate:
    id = "G2"
    budget_s = BUILD_TIMEOUT_S + 60
    OPTIONS: tuple[str, ...] = ()

    def run(self, ctx: GateContext) -> GateResult:
        requirements = list(ctx.package.manifest.requirements)
        reused = ctx.version.blob_sha if ctx.version is not None else None
        if reused is not None and ctx.registry is not None and ctx.registry.blobs.exists(reused):
            blob = ctx.registry.blobs.get(reused)
            return self._seal(ctx, blob, requirements, 0, "", reused=True)
        if not requirements:
            return self._seal(ctx, ctx.package.to_tar(), requirements, 0, "")
        return self._install(ctx, requirements)

    # -- with dependencies ----------------------------------------------------------------------------------------------

    def _install(self, ctx: GateContext, requirements: list[str]) -> GateResult:
        hosts = tuple(ctx.settings.build_allow)
        if not hosts:
            return failed(
                "the operator has not allowed any package index, so dependencies cannot be installed",
                [
                    "ask the operator to set TOOLFORGE_BUILD_ALLOW (for example pypi.org,files.pythonhosted.org), or submit a tool without requirements"
                ],
            )
        seed = ctx.package.to_tar()
        with sandboxrun.open_sandbox(ctx, "G2", network=hosts) as handle:
            packed, _ = repack_blob(seed)
            handle.thaw(packed)
            handle.put(REQUIREMENTS_PATH, ("\n".join(requirements) + "\n").encode("utf-8"))
            output = handle.run(PIP_ARGV, int(min(BUILD_TIMEOUT_S, max(10, ctx.remaining_s()))), PIP_MAX_OUT)
            text = (output.stdout + b"\n" + output.stderr).decode("utf-8", "replace")
            tail = textsafety.sanitize(text[-PIP_OUTPUT_CHARS:], max_chars=PIP_OUTPUT_CHARS)
            if output.timed_out:
                return failed(
                    f"installing the requirements took longer than {BUILD_TIMEOUT_S} seconds",
                    ["use fewer or smaller dependencies"],
                    metrics={"requirements": len(requirements)},
                    detail={"pip_output_tail": tail},
                )
            if output.exit_code != 0:
                return failed(
                    f"pip could not install the requirements (exit code {output.exit_code})",
                    pip_hints(text, requirements),
                    metrics={"requirements": len(requirements)},
                    detail={"pip_output_tail": tail},
                )
            blob = handle.freeze()
        return self._seal(ctx, blob, requirements, len(requirements), tail)

    # -- the blob ---------------------------------------------------------------------------------------------------------

    def _seal(
        self,
        ctx: GateContext,
        blob: bytes,
        requirements: list[str],
        installed: int,
        tail: str,
        *,
        reused: bool = False,
    ) -> GateResult:
        """Check the blob the way a call reads it, store it, attach it, and hand it to the next gates."""
        try:
            _, names = repack_blob(blob)
        except ToolNotRunnable as exc:
            return failed(
                "the built package cannot be used",
                [
                    clip(exc.message, 180),
                    "the workspace is 64 MB and a package may fill three quarters of it; use fewer or smaller dependencies",
                ],
                metrics={"requirements": len(requirements)},
                detail={"pip_output_tail": tail},
            )
        entry = ctx.package.manifest.entrypoint.rpartition(":")[0]
        if entry not in names:
            return failed(
                f"the entrypoint file {clip(entry, 60)} is not in the built package",
                ["check manifest.entrypoint names a file that is in the package"],
            )
        dependency_files = sum(
            1 for name in names if name.startswith("pkg/.deps/") or name.startswith(".deps/")
        )
        if ctx.registry is not None and ctx.version is not None:
            sha = ctx.registry.blobs.put(blob)
            ctx.registry.attach_blob(ctx.version.id, sha)
        else:
            sha = hashlib.sha256(blob).hexdigest()
        ctx.blob, ctx.blob_sha = blob, sha
        metrics: dict[str, Any] = {
            "requirements": len(requirements),
            "installed_files": dependency_files,
            "package_files": len(names) - dependency_files,
            "blob_bytes": len(blob),
        }
        what = (
            "reused the frozen package from the earlier run"
            if reused
            else (
                f"installed {len(requirements)} requirement{'s' if len(requirements) != 1 else ''} from wheels and froze the package"
                if requirements
                else "no requirements to install; froze the package as it is"
            )
        )
        return passed(
            what,
            metrics=metrics,
            detail={"blob_sha": sha, "pip_output_tail": tail, "reused": reused},
        )


__all__ = ["BUILD_TIMEOUT_S", "PIP_ARGV", "BuildGate", "pip_hints"]
