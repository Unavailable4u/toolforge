"""docs/verification.md says what the code does (the drift habit from docs/guide/01-conventions.md)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from toolforge.verify import g2_build, g3_selftest, g4_fuzz
from toolforge.verify.g1_static import FINDING_CODES, StaticGate, finding_codes_markdown
from toolforge.verify.pipeline import GATE_INFO, default_gates, gates_markdown
from toolforge.verify.secretscan import PATTERN_NAMES, patterns_markdown

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "verification.md"


def embedded(name: str) -> str:
    match = re.search(
        rf"<!-- {name}:begin -->\n(.*?)\n<!-- {name}:end -->", DOC.read_text(encoding="utf-8"), re.DOTALL
    )
    assert match, f"docs/verification.md must wrap a table in {name}:begin / {name}:end markers"
    return match.group(1)


@pytest.mark.parametrize(
    ("marker", "make"),
    [
        ("gates", gates_markdown),
        ("g1-codes", finding_codes_markdown),
        ("secret-patterns", patterns_markdown),
    ],
)
def test_generated_tables_match_the_code(marker: str, make) -> None:
    assert embedded(marker) == make(), (
        f"docs/verification.md is out of date: replace the text between the {marker} markers with the generated table"
    )


def test_every_finding_code_and_pattern_is_listed() -> None:
    assert all(f"`{code}`" in embedded("g1-codes") for code in FINDING_CODES)
    assert all(f"`{name}`" in embedded("secret-patterns") for name in PATTERN_NAMES)


def test_the_documented_time_budgets_are_the_real_ones() -> None:
    budgets = {gate.id: gate.budget_s for gate in default_gates()}
    text = DOC.read_text(encoding="utf-8")
    assert f"| G1 | {budgets['G1']} seconds |" in text
    assert (
        f"| G2 | {budgets['G2']} seconds (the pip step itself is limited to {g2_build.BUILD_TIMEOUT_S}) |"
        in text
    )
    assert (
        f"| G3 | {budgets['G3']} seconds (tests {g3_selftest.TESTS_DEADLINE_S}, examples {g3_selftest.EXAMPLES_DEADLINE_S}, strength check {g3_selftest.TESTS_DEADLINE_S}) |"
        in text
    )
    assert f"| G4 | {budgets['G4']} seconds (inputs {g4_fuzz.BUDGET_S}) |" in text
    assert f"(1 to {g4_fuzz.MAX_INPUTS}, default {g4_fuzz.DEFAULT_INPUTS})" in text


def test_the_documented_pip_command_is_the_real_one() -> None:
    text = DOC.read_text(encoding="utf-8")
    command = " ".join(g2_build.PIP_ARGV[g2_build.PIP_ARGV.index("install") + 1 :])
    for flag in (
        "--only-binary=:all:",
        "--no-deps",
        "--no-compile",
        "--no-cache-dir",
        "--target /workspace/pkg/.deps",
    ):
        assert flag in command and flag in text, flag


def test_the_gate_listing_in_the_docs_has_the_right_status() -> None:
    assert {g.id for g in GATE_INFO if g.status == "built"} == {"G1", "G2", "G3", "G4"}
    assert StaticGate.id == "G1"
