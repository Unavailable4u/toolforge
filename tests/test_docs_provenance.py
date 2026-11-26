"""`docs/PROVENANCE.md` and the code agree: every error code the provenance modules can raise is in the page's table, and the table lists no other."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [
    *sorted((ROOT / "src" / "toolforge" / "provenance").glob("*.py")),
    ROOT / "src" / "toolforge" / "cli_provenance.py",
]


def _codes_in_code() -> set[str]:
    return {m for path in SOURCES for m in re.findall(r'code="([a-z_]+)"', path.read_text(encoding="utf-8"))}


def _codes_in_page() -> set[str]:
    page = (ROOT / "docs" / "PROVENANCE.md").read_text(encoding="utf-8")
    table = page.split("## Errors", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE))


def test_the_error_table_is_the_set_of_codes_in_the_code() -> None:
    assert _codes_in_code() == _codes_in_page()


def test_the_page_says_what_a_signature_does_not_mean() -> None:
    page = (ROOT / "docs" / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "does **not** mean" in page and "never skips a gate" in page and "Not measured" in page


def test_the_documented_limits_are_the_real_ones() -> None:
    from toolforge.provenance import attest, bundle, keys

    page = (ROOT / "docs" / "PROVENANCE.md").read_text(encoding="utf-8")
    assert bundle.MAX_BUNDLE_BYTES == 64 * 1024 * 1024 and "at most 64 MB" in page
    assert attest.MAX_ATTESTATION_BYTES == 64 * 1024 and "| 64 KB |" in page
    assert keys.MAX_TRUSTED_KEYS == 500 and "at most 500 keys" in page
    assert bundle.MAX_LEDGER_EVENTS == 100_000 and "100 000 events" in page
