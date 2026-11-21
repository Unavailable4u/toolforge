"""MiniLocker is imported in five named modules only, and never at module level, so `import toolforge` needs no Docker.

P08 added `report/blast_radius.py` (it reaches MiniLocker's report builder, which needs no Docker). A later patch may extend
ALLOWED_IMPORTERS with `sandbox/browser_adapter.py` (P14), in its own patch and with a dated line in docs/guide/DEVIATIONS.md.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
ALLOWED_IMPORTERS = {
    "toolforge/sandbox/_ml_path.py",
    "toolforge/sandbox/minilocker_runner.py",
    "toolforge/ledger.py",
    "toolforge/report/blast_radius.py",
    "toolforge/textsafety.py",
}
# Third-party packages that only a real sandbox needs; they are imported inside functions, never at module level.
LAZY_ONLY_ROOTS = {"minilocker", "docker", "requests"}


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def _roots(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


def _module_level_nodes(tree: ast.Module) -> list[ast.AST]:
    """Every node that runs when the module is imported: not inside a function or a lambda (class bodies do run)."""
    found: list[ast.AST] = []
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        found.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return found


def test_src_is_not_empty() -> None:
    assert len(_modules()) > 10  # a broken glob must not make the other tests pass vacuously
    assert ALLOWED_IMPORTERS <= {name for name, _ in _modules()}


def test_only_the_named_modules_import_minilocker() -> None:
    offenders = [
        name
        for name, tree in _modules()
        if name not in ALLOWED_IMPORTERS and any("minilocker" in _roots(node) for node in ast.walk(tree))
    ]
    assert offenders == []


def test_only_the_named_modules_ask_for_minilocker_by_name() -> None:
    """MiniLocker is also reached as a string (`_ml_path.load("minilocker.x")`); that needs the same fence."""
    offenders = []
    for name, tree in _modules():
        if name in ALLOWED_IMPORTERS:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("minilocker.")
            ):
                if not _is_docstring(tree, node):
                    offenders.append(f"{name}: {node.value}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"ensure_importable"}
            ):
                offenders.append(f"{name}: ensure_importable")
    assert offenders == []


def _is_docstring(tree: ast.Module, node: ast.Constant) -> bool:
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and body[0].value is node:
            return True
    return False


def test_minilocker_and_docker_are_never_imported_at_module_level() -> None:
    offenders = [
        f"{name}: {root}"
        for name, tree in _modules()
        for node in _module_level_nodes(tree)
        for root in _roots(node)
        if root in LAZY_ONLY_ROOTS
    ]
    assert offenders == []


def test_importing_toolforge_does_not_load_minilocker_or_docker() -> None:
    """The proof that matters, in a fresh interpreter: every module that exists, imported, pulls in neither."""
    modules = [name[:-3].replace("/", ".").removesuffix(".__init__") for name, _ in _modules()]
    code = (
        "import importlib, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in {'minilocker', 'docker'})\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
