"""`toolforge validate` and `toolforge new`: check a package on disk, and scaffold a valid one.

Neither command runs package code. `validate` reads files and parses Python with `ast` only (`ToolPackage.from_dir`).
`new` writes a fixed template and then validates its own output, so the scaffold can never drift out of the rules
(`tests/test_cli_package.py::test_new_scaffold_validates`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from toolforge.config import load_settings
from toolforge.errors import ToolForgeError
from toolforge.models.package import ToolPackage, name_problem
from toolforge.models.results import Issue, ValidationReport

_DESCRIPTION = (
    "Normalise whitespace in a piece of text and count its words. "
    "Replace this sentence with what your tool really does, in plain language."
)

# The scaffold is a tiny but real tool with three tests that assert on returned values, so a fresh package also passes the
# test-strength check of the verification pipeline (P06), which fails tests that still pass when the tool returns nothing.
_TOOL_PY = '''"""Replace this module with your tool. `run` receives the validated input and returns any JSON-serialisable value."""

from __future__ import annotations


def run(args: dict) -> dict:
    words = args["text"].split()
    return {"text": " ".join(words), "words": len(words)}
'''

_TEST_PY = """from tool import run


def test_collapses_runs_of_whitespace():
    assert run({"text": "  a \\t b\\n\\nc  "}) == {"text": "a b c", "words": 3}


def test_empty_text_has_no_words():
    assert run({"text": ""}) == {"text": "", "words": 0}


def test_unicode_words_are_kept_intact():
    result = run({"text": "caf\\u00e9   cr\\u00e8me"})
    assert result["text"] == "caf\\u00e9 cr\\u00e8me"
    assert result["words"] == 2
"""


def scaffold(name: str) -> ToolPackage:
    """A valid starter package called `name`."""
    problem = name_problem(name)
    if problem:
        raise ToolForgeError(
            f"cannot create a tool called {name!r}: {problem[1]}",
            code=problem[0],
            hint="use 3 to 41 characters: a lower-case letter first, then lower-case letters, digits or underscores; "
            "do not start with forge_ or tf_",
        )
    manifest = {
        "name": name,
        "version": "0.1.0",
        "parent_version": None,
        "description": _DESCRIPTION,
        "when_to_use": "The task gives text and asks for it cleaned up or for a word count.",
        "entrypoint": "tool.py:run",
        "kind": "stateless",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "words": {"type": "integer"}},
            "required": ["text", "words"],
        },
        "permissions": {"network": [], "secrets": [], "inputs": False, "browser": False},
        "limits": {"timeout_s": 10, "memory_mb": 128, "output_kb": 16},
        "requirements": [],
        "examples": [{"input": {"text": "  hello   world "}, "output": {"text": "hello world", "words": 2}}],
    }
    return ToolPackage(manifest, {"tool.py": _TOOL_PY, "tests/test_tool.py": _TEST_PY})


def _format_issue(kind: str, issue: Issue) -> str:
    lines = [f"{kind} [{issue.code}] {issue.path}: {issue.message}"]
    if issue.hint:
        lines.append(f"  hint: {issue.hint}")
    return "\n".join(lines)


def _report_text(report: ValidationReport) -> str:
    parts = [_format_issue("error", issue) for issue in report.issues]
    parts += [_format_issue("warning", issue) for issue in report.warnings]
    errors, warnings = len(report.issues), len(report.warnings)
    verdict = "valid" if report.ok else "INVALID"
    parts.append(
        f"{verdict}: {errors} error{'s' if errors != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}"
    )
    return "\n".join(parts)


def _cmd_validate(args: argparse.Namespace) -> int:
    package = ToolPackage.from_dir(args.dir)
    report = package.validate(load_settings())
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print(_report_text(report))
    return 0 if report.ok else 1


def _cmd_new(args: argparse.Namespace) -> int:
    package = scaffold(args.name)
    report = package.validate(load_settings())
    if not report.ok:  # a bug in the template, not in the user's input
        raise ToolForgeError(
            "the built-in template does not validate: " + "; ".join(i.message for i in report.issues),
            code="template_invalid",
            hint="this is a ToolForge bug; please report it",
        )
    destination = Path(args.dir) / args.name
    package.to_dir(destination)
    print(f"created {destination}")
    print(f"next: toolforge validate {destination}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    validate = subparsers.add_parser(
        "validate",
        help="check a tool package folder without running it",
        description="Read a package folder and report every rule it breaks. Package code is never run or imported.",
    )
    validate.add_argument("dir", help="the folder that contains manifest.json")
    validate.add_argument("--json", action="store_true", help="print the report as JSON")
    validate.set_defaults(handler=_cmd_validate)

    new = subparsers.add_parser(
        "new",
        help="create a valid starter tool package",
        description="Write a starter package (manifest, entrypoint and three tests) into <dir>/<name>.",
    )
    new.add_argument("name", help="the tool name, for example word_tools")
    new.add_argument(
        "--dir", default=".", help="where to create the package folder (default: the current folder)"
    )
    new.set_defaults(handler=_cmd_new)


__all__ = ["register", "scaffold"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this through the `toolforge` command")
