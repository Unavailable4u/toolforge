"""Gate G1, static: read every file without running it, and report every problem at once.

What it does, in order: everything `ToolPackage.validate` already checks (P02), then `ast` analysis of every `.py` file, then a scan of
every file (and the manifest) for secret-shaped strings, then a scan of the description, `when_to_use` and the examples' text for
instructions aimed at an AI. Nothing is imported or executed.

This gate is advisory and says so in its summary: it is trivially bypassed (`getattr(__builtins__, "ev" + "al")`, a string built at run
time, a native extension). It catches honest mistakes and lazy attempts early and cheaply. The sandbox is the real boundary, and
G6 (P08) is the check that tests it.

Never raises: the package is hostile input, so any unexpected error becomes a failed gate with a generic message (`tests/test_g1_static.py`
feeds it random garbage). The matched text of a secret is never copied anywhere: a finding is a pattern name, a file and a line.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from toolforge import textsafety
from toolforge.models.package import ToolPackage
from toolforge.verify import secretscan
from toolforge.verify.pipeline import GateContext, GateResult, clip, failed, passed

MAX_FINDINGS = 60  # recorded findings; the counts in the metrics are exact
MAX_PY_BYTES = (
    1_000_000  # a file larger than this is not parsed (a package is capped far below it by validation)
)
MAX_AST_NODES = 200_000  # a bomb of nested expressions cannot make the walk long

# Modules whose import means "this tool wants a network". The first three words are as the guide lists them.
NETWORK_MODULES = (
    "socket",
    "ssl",
    "http",
    "urllib.request",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
    "websockets",
)
NATIVE_MODULES = ("ctypes", "pty")
PROCESS_MODULES = ("subprocess",)
DYNAMIC_BUILTINS = ("eval", "exec", "compile")
PROCESS_CALLS = ("os.system", "os.popen")
ENVIRONMENT_NAMES = ("os.environ", "os.environb", "os.getenv", "os.getenvb")
_SENSITIVE_PATH = re.compile(r"\s*(?:/etc|/proc|/root)(?:/|\s*$)|\s*~/\.(?:ssh|aws)(?:/|\s*$)")


class FindingInfo(NamedTuple):
    severity: str  # "fail" rejects the package; "warn" is shown and stored
    rule: str  # as shown in the docs
    hint: str


FINDING_CODES: dict[str, FindingInfo] = {
    "network_import": FindingInfo(
        "fail",
        "The code imports a networking library (`socket`, `ssl`, `http`, `urllib.request`, `urllib3`, `requests`, `httpx`, "
        "`aiohttp`, `ftplib`, `smtplib`, `telnetlib`, `websockets`) while `permissions.network` is empty.",
        "declare the host in permissions.network, or remove the import: a tool without declared hosts has no network",
    ),
    "native_code": FindingInfo(
        "fail",
        "The code imports `ctypes` or `pty`.",
        "remove it: tools run as pure Python, and native calls or pseudo-terminals are not allowed",
    ),
    "secret": FindingInfo(
        "fail",
        "A string in any file looks like a key or token (see the pattern list below). The matched text is never shown.",
        "remove it and read secrets through a name declared in permissions.secrets; use obviously fake values in tests",
    ),
    "injection_text": FindingInfo(
        "fail",
        "The description, `when_to_use` or example text looks like instructions aimed at an AI.",
        "rewrite it as plain text that says what the tool does and when to use it; leave out orders and role changes",
    ),
    "dynamic_code": FindingInfo(
        "warn",
        "`eval`, `exec` or `compile` is called, or `__import__` / `importlib.import_module` gets a non-literal name.",
        "avoid running text as code; if you need it, say why in the description",
    ),
    "process_spawn": FindingInfo(
        "warn",
        "The code imports `subprocess` or calls `os.system` or `os.popen`.",
        "prefer library calls; starting programs is limited by the sandbox and may fail there",
    ),
    "environment_read": FindingInfo(
        "warn",
        "The code reads `os.environ` or calls `os.getenv`.",
        "tools get no secrets in the environment; declare a secret in permissions.secrets instead",
    ),
    "sensitive_path": FindingInfo(
        "warn",
        "A string names a path under `/etc`, `/proc`, `/root`, `~/.ssh` or `~/.aws`.",
        "tools work in /workspace; they have no reason to read system or credential folders",
    ),
}


@dataclass(frozen=True)
class Finding:
    code: str
    file: str
    line: int
    detail: str  # a short fixed phrase built by this module, never text copied from the package

    @property
    def severity(self) -> str:
        return FINDING_CODES[self.code].severity

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "file": clip(self.file, 80),
            "line": self.line,
            "detail": self.detail,
        }

    def sentence(self) -> str:
        return f"{clip(self.file, 80)}:{self.line} {self.detail}"


def finding_codes_markdown() -> str:
    """The finding list as a Markdown table. `docs/verification.md` embeds exactly this text (drift-tested)."""
    rows = ["| Code | Effect | Rule | What to do |", "| --- | --- | --- | --- |"]
    rows += [
        f"| `{code}` | {'rejects' if info.severity == 'fail' else 'warns'} | {info.rule} | {info.hint} |"
        for code, info in FINDING_CODES.items()
    ]
    return "\n".join(rows)


# --- Reading a file's syntax tree ----------------------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` for a chain of names and attributes, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _matches(name: str, modules: tuple[str, ...]) -> str | None:
    for module in modules:
        if name == module or name.startswith(module + "."):
            return module
    return None


def _import_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Every dotted module name an import statement brings in (`from urllib import request` gives `urllib` and `urllib.request`)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level or not node.module:
        return []
    return [node.module, *[f"{node.module}.{alias.name}" for alias in node.names]]


class _FileScan:
    def __init__(self, file: str, network_allowed: bool) -> None:
        self.file = file
        self.network_allowed = network_allowed
        self.findings: list[Finding] = []
        self.imported_names: set[str] = set()

    def add(self, code: str, node: ast.AST, detail: str) -> None:
        self.findings.append(Finding(code, self.file, getattr(node, "lineno", 1) or 1, detail))

    def run(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):  # first, the names that `from x import name` made local
            if isinstance(node, ast.ImportFrom):
                self.imported_names.update(a.asname or a.name for a in node.names)
        for count, node in enumerate(ast.walk(tree)):
            if count > MAX_AST_NODES:
                return
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._import(node)
            elif isinstance(node, ast.Call):
                self._call(node)
            elif isinstance(node, ast.Attribute):
                self._attribute(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if len(node.value) <= 4096 and _SENSITIVE_PATH.match(node.value):
                    self.add("sensitive_path", node, "names a system or credential folder")

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        for name in _import_names(node):
            if (module := _matches(name, NATIVE_MODULES)) is not None:
                self.add("native_code", node, f"imports `{module}`")
            elif _matches(name, PROCESS_MODULES) is not None:
                self.add("process_spawn", node, "imports `subprocess`")
            elif not self.network_allowed and (module := _matches(name, NETWORK_MODULES)) is not None:
                self.add("network_import", node, f"imports `{module}` but permissions.network is empty")
        if isinstance(node, ast.ImportFrom) and not node.level and node.module == "os":
            for alias in node.names:
                if alias.name in ("environ", "environb", "getenv", "getenvb"):
                    self.add("environment_read", node, f"imports `{alias.name}` from `os`")
                elif alias.name in ("system", "popen"):
                    self.add("process_spawn", node, f"imports `{alias.name}` from `os`")

    def _call(self, node: ast.Call) -> None:
        func = _dotted(node.func)
        if func is None:
            return
        if func in DYNAMIC_BUILTINS and func not in self.imported_names:
            self.add("dynamic_code", node, f"calls `{func}`")
        elif func in ("__import__", "importlib.import_module", "import_module"):
            literal = (
                node.args[0].value
                if node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                else None
            )
            if literal is None:
                self.add("dynamic_code", node, f"calls `{func}` with a name that is not a literal")
            elif (module := _matches(literal, NATIVE_MODULES)) is not None:
                self.add("native_code", node, f"imports `{module}` by name")
            elif not self.network_allowed and (module := _matches(literal, NETWORK_MODULES)) is not None:
                self.add(
                    "network_import", node, f"imports `{module}` by name but permissions.network is empty"
                )
        elif func in PROCESS_CALLS:
            self.add("process_spawn", node, f"calls `{func}`")
        elif func in ENVIRONMENT_NAMES:
            self.add("environment_read", node, f"calls `{func}`")

    def _attribute(self, node: ast.Attribute) -> None:
        if _dotted(node) in ("os.environ", "os.environb"):
            self.add("environment_read", node, "reads `os.environ`")


def _scan_python(file: str, text: str, network_allowed: bool) -> list[Finding]:
    if len(text) > MAX_PY_BYTES:
        return []  # not parsed; validation already rejects files this large
    try:
        tree = ast.parse(text, filename=file)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []  # P02 validation reports the syntax error
    scan = _FileScan(file, network_allowed)
    try:
        scan.run(tree)
    except RecursionError:
        return scan.findings
    return scan.findings


# --- Text that talks to a model ------------------------------------------------------------------------------------------


def _strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 8 or len(out) > 500:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in list(value.values())[:200]:
            _strings(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value[:200]:
            _strings(item, out, depth + 1)


def _prose(package: ToolPackage) -> dict[str, str]:
    """The manifest text a model reads: description, when_to_use and every string in the examples (inputs, files, outputs)."""
    manifest = package.manifest
    texts: dict[str, str] = {"description": manifest.description, "when_to_use": manifest.when_to_use}
    for number, example in enumerate(manifest.examples):
        strings: list[str] = []
        _strings([example.input, example.files, example.output], strings)
        texts[f"examples[{number}]"] = "\n".join(strings)[:20_000]
    return texts


# --- The gate ------------------------------------------------------------------------------------------------------------


def _manifest_text(package: ToolPackage) -> str | None:
    try:
        return json.dumps(package.manifest.model_dump(mode="json"), indent=1, ensure_ascii=False)
    except Exception:
        return None


def analyse(package: ToolPackage, settings: Any) -> tuple[list[Finding], list[str], dict[str, Any]]:
    """`(findings, validation lines, metrics)`. Separate from `run` so tests can look at the raw findings."""
    report = package.validate(settings)
    lines: list[str] = []
    for issue in report.issues:
        text = f"[{issue.code}] {issue.path}: {issue.message}"
        lines.append(clip(text + (f" Fix: {issue.hint}" if issue.hint else ""), 300))
    warnings = [clip(f"[{w.code}] {w.path}: {w.message}", 300) for w in report.warnings]
    findings: list[Finding] = []
    network_allowed = False
    try:
        network_allowed = bool(package.manifest.permissions.network)
    except Exception:
        pass
    python_files = 0
    for path in sorted(package.files):
        text = package.files[path]
        if not isinstance(text, str):
            continue
        if path.endswith(".py"):
            python_files += 1
            findings += _scan_python(path, text, network_allowed)
        for hit in secretscan.scan(text, path):
            findings.append(Finding("secret", hit.file, hit.line, f"looks like a secret ({hit.pattern})"))
    manifest_text = _manifest_text(package)
    if manifest_text is not None:
        for hit in secretscan.scan(manifest_text, "manifest.json"):
            findings.append(Finding("secret", hit.file, hit.line, f"looks like a secret ({hit.pattern})"))
    injection_measured: bool | None = None
    if manifest_text is not None:
        injection_measured = True
        for where, text in _prose(package).items():
            result = textsafety.scan(text)
            if not result.get("measured"):
                injection_measured = False
            elif result.get("suspected"):
                labels = sorted(
                    {clip(str(f.get("label", f.get("rule", ""))), 60) for f in result.get("findings", [])}
                )
                findings.append(
                    Finding(
                        "injection_text",
                        "manifest.json",
                        1,
                        f"{where} looks like instructions to an AI: {', '.join(labels[:3])}",
                    )
                )
    metrics = {
        "validation_errors": len(report.issues),
        "python_files": python_files,
        "files": len(package.files),
        "failures": sum(1 for f in findings if f.severity == "fail"),
        "warnings": sum(1 for f in findings if f.severity == "warn") + len(warnings),
        "secret_findings": sum(1 for f in findings if f.code == "secret"),
        "injection_scan": "not measured" if injection_measured is False else "measured",
    }
    return findings, lines + [f"warning:{w}" for w in warnings], metrics


class StaticGate:
    id = "G1"
    budget_s = 60
    OPTIONS: tuple[str, ...] = ()

    def run(self, ctx: GateContext) -> GateResult:
        try:
            return self._run(ctx)
        except Exception as exc:  # hostile input must never take the pipeline down
            return failed(
                f"G1 could not read the package safely ({type(exc).__name__})",
                ["check the package with `toolforge validate <dir>` and remove anything unusual"],
            )

    def _run(self, ctx: GateContext) -> GateResult:
        findings, lines, metrics = analyse(ctx.package, ctx.settings)
        problems = [line for line in lines if not line.startswith("warning:")]
        notes = [line[len("warning:") :] for line in lines if line.startswith("warning:")]
        notes += [f.sentence() for f in findings if f.severity == "warn"]
        if metrics["injection_scan"] == "not measured":
            notes.append(
                "the instruction-text scan did not run (MiniLocker is not available), so descriptions were not checked"
            )
        detail: dict[str, Any] = {
            "findings": [f.as_dict() for f in findings][:MAX_FINDINGS],
            "validation": problems[:MAX_FINDINGS],
            "warnings": notes[:MAX_FINDINGS],
            "advisory": True,
        }
        fails = [f for f in findings if f.severity == "fail"]
        if problems or fails:
            hints = list(problems)
            seen: set[str] = set()
            for finding in fails:
                hints.append(f"{finding.sentence()}. {FINDING_CODES[finding.code].hint}")
                seen.add(finding.code)
            count = len(problems) + len(fails)
            return failed(
                f"G1 found {count} problem{'s' if count != 1 else ''} (every one is listed in the hints); static checks are advisory",
                hints,
                metrics=metrics,
                detail=detail,
            )
        return passed(
            "no blocking problems found; static checks are advisory and easy to get around, the sandbox is the real boundary",
            metrics=metrics,
            detail=detail,
        )


__all__ = [
    "FINDING_CODES",
    "NETWORK_MODULES",
    "Finding",
    "StaticGate",
    "analyse",
    "finding_codes_markdown",
]
