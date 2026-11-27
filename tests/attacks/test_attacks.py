"""The attack lab against real sandboxes. Needs Docker and the built runtime image (`toolforge images build`); skipped otherwise.

Every attack in the catalogue is driven for real and must end `contained`, or `inconclusive` with a reason the verdict names (the reason is
recorded with the test, so a quiet pass cannot hide a gap). A `breached` verdict fails the test with the evidence, which is the point of
the suite: a defence that stops holding shows up here. The run-wide checks (host canary, ledger chains, sandboxes destroyed, no container
left behind) are part of every attack's verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from toolforge.attacks import runner
from toolforge.attacks.catalog import BREACHED, CONTAINED, INCONCLUSIVE, SKIPPED, attack_ids
from toolforge.config import Settings

pytestmark = pytest.mark.attacks

EXPLAINED = ("unproven:", "it ran past", "the attack driver failed", "the sandbox failed")


@pytest.fixture(scope="module", autouse=True)
def _the_image_is_built() -> None:
    problem = runner.docker_problem(Settings())
    if problem:
        pytest.skip(problem)


@pytest.mark.parametrize("attack_id", attack_ids())
def test_attack(attack_id: str, record_property: Callable[[str, Any], None]) -> None:
    [result] = runner.run_attacks([attack_id], deadline_s=runner.DEADLINE_S)
    open_questions = [
        (c.label, c.status, c.detail) for c in result.checks if c.status not in ("pass", "skip")
    ]
    record_property("verdict", result.verdict)
    record_property("evidence", result.evidence)
    if result.verdict == SKIPPED:
        pytest.skip(result.evidence)
    assert result.verdict != BREACHED, f"{attack_id} was BREACHED: {result.evidence} {open_questions}"
    if result.verdict == INCONCLUSIVE:
        assert result.evidence.startswith(EXPLAINED), (
            f"{attack_id} is inconclusive without a reason: {result.evidence}"
        )
        return
    assert result.verdict == CONTAINED, (result.verdict, result.evidence, open_questions)
