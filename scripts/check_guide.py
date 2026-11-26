"""Consistency checks for the build guide, run by hand or as a test (P01 adds tests/test_guide_consistency.py).

The guide is a plan that many patches edit. This script catches the mistakes that creep in when a plan grows: a patch with
no row in PROGRESS.md, a "Depends on" line that disagrees with the table, a file named in the index that does not exist,
a reference to a patch that was never defined, Windows line endings, and half-removed old wording.

Standard library only. Exit code 0 when everything is consistent, 1 otherwise. Usage:

    python scripts/check_guide.py            # check the repository this script lives in
    python scripts/check_guide.py --root .   # or name the root explicitly
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PATCH_ID = re.compile(r"\b([PS]\d{2}[a-z]?)\b")
RANGE = re.compile(r"\b(P\d{2}) to (P\d{2})\b")
TEXT_SUFFIXES = {
    ".md",
    ".json",
    ".py",
    ".yml",
    ".yaml",
    ".toml",
    ".html",
    ".css",
    ".js",
    ".svg",
    ".txt",
    ".cfg",
}
TEXT_NAMES = {".gitignore", ".gitattributes", ".editorconfig", "Makefile"}
SKIP_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "__pycache__",
    ".venv",
    "third_party",
    "runs",
}
REQUIRED_SECTIONS = ("Goal", "Scope", "Tests", "Acceptance")
# Files the index may name even though they do not exist at the start of the project.
MAY_BE_ABSENT = {"DEVIATIONS.md"}


def expand_ids(text: str) -> list[str]:
    """Patch ids named in a dependency phrase, expanding "P01 to P20" into the full run."""
    ids: list[str] = []
    for a, b in RANGE.findall(text):
        ids.extend(f"P{n:02d}" for n in range(int(a[1:]), int(b[1:]) + 1))
    ids.extend(PATCH_ID.findall(RANGE.sub("", text)))
    return sorted(set(ids))


def is_defined(ref: str, rows: dict[str, dict]) -> bool:
    """A patch id is defined if it has a row, or it names a split patch whose parts have rows (P14 is P14a and P14b)."""
    return ref in rows or any(r.startswith(ref) for r in rows)


def read_progress(root: Path) -> dict[str, dict]:
    """Rows of the PROGRESS.md table: id -> {title, deps, box}."""
    rows: dict[str, dict] = {}
    for line in (root / "PROGRESS.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"[PS]\d{2}[a-z]?", cells[1]):
            rows[cells[1]] = {"box": cells[0], "title": cells[2], "deps": cells[3]}
    return rows


def patch_files(root: Path) -> dict[str, Path]:
    return {p.name.split("-", 1)[0]: p for p in sorted((root / "docs/guide/patches").glob("*.md"))}


def check_patches(root: Path, errors: list[str]) -> dict[str, dict]:
    rows = read_progress(root)
    files = patch_files(root)
    if not rows:
        errors.append("PROGRESS.md: no patch rows found")
    for pid in rows:
        if pid not in files:
            errors.append(f"PROGRESS.md lists {pid} but docs/guide/patches/ has no file for it")
    for pid in files:
        if pid not in rows:
            errors.append(f"docs/guide/patches/{files[pid].name} has no row in PROGRESS.md")
    for pid, path in files.items():
        if pid not in rows:
            continue
        text = path.read_text(encoding="utf-8")
        first = text.splitlines()[0] if text else ""
        match = re.fullmatch(r"# ([PS]\d{2}[a-z]?): (.+)", first)
        if not match or match.group(1) != pid:
            errors.append(f"{path.name}: first line must be '# {pid}: <title>' (found {first!r})")
        elif match.group(2).strip() != rows[pid]["title"]:
            errors.append(
                f"{path.name}: title {match.group(2).strip()!r} differs from PROGRESS.md {rows[pid]['title']!r}"
            )
        dep = re.search(r"^Depends on:\s*(.+)$", text, re.MULTILINE)
        if not dep:
            errors.append(f"{path.name}: no 'Depends on:' line")
        else:
            first_sentence = re.split(r"\.\s", dep.group(1) + " ", maxsplit=1)[0]
            in_file = expand_ids(first_sentence)
            in_table = expand_ids(rows[pid]["deps"])
            if in_file != in_table:
                errors.append(
                    f"{pid}: 'Depends on' is {in_file} in the patch file but {in_table} in PROGRESS.md"
                )
        for section in REQUIRED_SECTIONS:
            if not re.search(rf"^## {section}\b", text, re.MULTILINE):
                errors.append(f"{path.name}: missing a '## {section}' section")
        for ref in expand_ids(dep.group(1)) if dep else []:
            if not is_defined(ref, rows):
                errors.append(f"{pid}: depends on {ref}, which is not defined")
    for pid, row in rows.items():
        for ref in expand_ids(row["deps"]):
            if not is_defined(ref, rows):
                errors.append(f"PROGRESS.md: {pid} depends on {ref}, which is not defined")
            elif ref == pid:
                errors.append(f"PROGRESS.md: {pid} depends on itself")
    check_cycles(rows, errors)
    return rows


def check_cycles(rows: dict[str, dict], errors: list[str]) -> None:
    graph = {pid: expand_ids(r["deps"]) for pid, r in rows.items()}
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            errors.append("dependency cycle: " + " -> ".join(trail + [node]))
            return
        state[node] = 1
        for dep in graph.get(node, []):
            if dep in graph:
                visit(dep, trail + [node])
        state[node] = 2

    for pid in graph:
        visit(pid, [])


def check_order(rows: dict[str, dict], errors: list[str]) -> None:
    """AGENTS.md picks 'the first unticked row whose dependencies are ticked', so rows must list dependencies first."""
    seen: set[str] = set()
    for pid, row in rows.items():
        for dep in expand_ids(row["deps"]):
            if dep in rows and dep not in seen:
                errors.append(f"PROGRESS.md: {pid} appears before its dependency {dep}")
        seen.add(pid)


def check_index(root: Path, rows: dict[str, dict], errors: list[str]) -> None:
    index = (root / "docs/guide/00-index.md").read_text(encoding="utf-8")
    guide = root / "docs/guide"
    for name in sorted(set(re.findall(r"`([A-Za-z0-9_.\-]+\.md)`", index))):
        if name in MAY_BE_ABSENT:
            continue
        if not any((base / sub / name).exists() for base in (guide, root) for sub in ("", "patches")):
            errors.append(
                f"00-index.md names `{name}` but no such file exists in docs/guide/ or the repository root"
            )
    for pid in rows:
        if not re.search(rf"^\| {pid} \|", index, re.MULTILINE):
            errors.append(f"00-index.md patch table has no row for {pid}")
    for pid in re.findall(r"^\| ([PS]\d{2}) \|", index, re.MULTILINE):
        if pid not in rows:
            errors.append(f"00-index.md lists {pid}, which is not in PROGRESS.md")


def check_references(root: Path, rows: dict[str, dict], errors: list[str]) -> None:
    """Every Pnn / Snn mentioned anywhere in the docs must exist (catches renumbering mistakes)."""
    for path in sorted(root.rglob("*.md")):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        if path.name in {"REVISIONS.md", "HANDOFF.md"}:
            continue  # these describe history, including old numbers
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pid in PATCH_ID.findall(line):
                if (
                    pid not in rows
                    and re.fullmatch(r"[PS]\d{2}", pid)
                    and not any(r.startswith(pid) for r in rows)
                ):  # "P14" names P14a and P14b together
                    errors.append(
                        f"{path.relative_to(root)}:{number}: mentions {pid}, which is not a defined patch"
                    )


def check_stale_wording(root: Path, errors: list[str]) -> None:
    """P19 used to be 'hardening and release' (now P21). Flag a phrase that still pairs them (within one table cell)."""
    pairing = re.compile(r"\bP19\b[^|]{0,60}hardening|hardening[^|]{0,60}\bP19\b", re.IGNORECASE)
    for path in sorted(root.rglob("*.md")):
        if SKIP_DIRS & set(path.relative_to(root).parts) or path.name in {
            "REVISIONS.md",
            "HANDOFF.md",
        }:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pairing.search(line):
                errors.append(
                    f"{path.relative_to(root)}:{number}: pairs P19 with hardening (that is P21 now)"
                )


def check_line_endings(root: Path, errors: list[str]) -> None:
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or SKIP_DIRS & set(rel.parts):
            continue
        is_text = path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES
        if is_text and b"\r" in path.read_bytes():
            errors.append(f"{rel}: contains a carriage return (CRLF); use LF")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    errors: list[str] = []
    rows = check_patches(root, errors)
    check_order(rows, errors)
    check_index(root, rows, errors)
    check_references(root, rows, errors)
    check_stale_wording(root, errors)
    check_line_endings(root, errors)
    if errors:
        print(f"{len(errors)} problem(s) in the guide:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(
        f"guide is consistent: {len(rows)} patches, dependencies, index, references and line endings all agree"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
