"""The documented issue codes and limits are the real ones (the drift habit from docs/guide/01-conventions.md)."""

from __future__ import annotations

import re
from pathlib import Path

from toolforge.models import package as package_module
from toolforge.models.package import ISSUE_CODES, issue_codes_markdown

DOC = Path(__file__).resolve().parent.parent / "docs" / "guide" / "package-format.md"


def _embedded_table() -> str:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"<!-- issue-codes:begin -->\n(.*?)\n<!-- issue-codes:end -->", text, re.DOTALL)
    assert match, "package-format.md must wrap the issue-code table in issue-codes:begin / end markers"
    return match.group(1)


def test_issue_code_table_matches_the_code() -> None:
    assert _embedded_table() == issue_codes_markdown(), (
        "docs/guide/package-format.md is out of date: replace the text between the issue-codes markers "
        "with the output of toolforge.models.package.issue_codes_markdown()"
    )


def test_every_code_names_a_rule_and_a_hint() -> None:
    for code, info in ISSUE_CODES.items():
        assert info.severity in {"error", "warning"}, code
        assert info.rule.strip() and info.hint.strip(), code
        assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", code), f"{code}: codes are dotted lower case"


def test_documented_numbers_are_the_enforced_numbers() -> None:
    text = DOC.read_text(encoding="utf-8")
    expected = [
        f"{package_module.DESCRIPTION_MIN} to {package_module.DESCRIPTION_MAX} characters",
        f"up to {package_module.WHEN_TO_USE_MAX} characters",
        f"At most {package_module.MAX_HOSTS} bare host names",
        f"At most {package_module.MAX_SECRETS} names",
        f"{package_module.TIMEOUT_RANGE[0]} to {package_module.TIMEOUT_RANGE[1]} seconds",
        f"{package_module.MEMORY_RANGE[0]} to {package_module.MEMORY_RANGE[1]}",
        f"{package_module.OUTPUT_RANGE[0]} to {package_module.OUTPUT_RANGE[1]}",
        f"At most {package_module.MAX_REQUIREMENTS}, each exactly",
        f"{package_module.MIN_EXAMPLES} to {package_module.MAX_EXAMPLES}. Every",
        f"At most {package_module.MAX_SCHEMA_DEPTH} levels of nested subschemas and {package_module.MAX_SCHEMA_PROPERTIES} properties",
        f"At most {package_module.MAX_FILES} files",
        f"at least {package_module.MIN_TESTS}".replace("at least 3", "at least three"),
    ]
    missing = [phrase for phrase in expected if phrase not in text]
    assert not missing, f"package-format.md no longer states: {missing}"
