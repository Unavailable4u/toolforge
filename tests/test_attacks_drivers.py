"""The attacks that need no sandbox run for real against the real code; each one is also broken on purpose to prove it can say `breached`.

The attacks that need Docker are driven by `tests/attacks/test_attacks.py`. Here they are only checked without one: their hostile packages are
valid and get past the gates they are meant to get past (the real G1 runs over each), and a sandbox that is not there never reads as a pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from toolforge import textsafety
from toolforge.attacks import catalog, runner
from toolforge.attacks.catalog import (
    BREACHED,
    CONTAINED,
    INCONCLUSIVE,
    SKIPPED,
    attack_ids,
    get_attack,
)
from toolforge.attacks.runner import Lab, run_one
from toolforge.errors import SandboxUnavailable
from toolforge.services.forge import Forge

NO_DOCKER = [a.id for a in catalog.ATTACKS if not a.needs_docker]
NEEDS_DOCKER = [a.id for a in catalog.ATTACKS if a.needs_docker]


@pytest.fixture
def lab(tmp_path: Path, minilocker: None) -> Iterator[Lab]:
    home = tmp_path / "home"
    home.mkdir()
    made = Lab(home, docker_ok=False)
    yield made
    made.close()


def judge(attack_id: str, lab: Lab) -> runner.AttackResult:
    return run_one(get_attack(attack_id), lab, deadline_s=120)


def details(result: runner.AttackResult) -> list[tuple[str, str, str]]:
    return [(c.label, c.status, c.detail) for c in result.checks if c.status != "pass"]


# --- The attacks that need no sandbox, for real ----------------------------------------------------------------------------------


def test_the_split_between_sandbox_and_no_sandbox_attacks_is_what_the_guide_says() -> None:
    assert set(NO_DOCKER) == {
        "poisoned_description",
        "name_squat",
        "approval_over_mcp",
        "secret_in_source",
        "http_no_token",
        "ledger_tamper",
        "plan_host_escape",
        "plan_vocabulary",
        "plan_page_text",
        "bundle_forgery",
        "bundle_stranger",
        "chain_laundering",
        "chain_ref_abuse",
    }


@pytest.mark.parametrize("attack_id", NO_DOCKER)
def test_the_defence_holds_against_the_real_stack(attack_id: str, lab: Lab) -> None:
    result = judge(attack_id, lab)
    assert result.verdict == CONTAINED, (result.evidence, details(result))
    assert {c.scope for c in result.checks} == {"attack", "always"}


# --- Breaking one defence at a time ----------------------------------------------------------------------------------------------


def test_name_squat_is_breached_when_the_collision_rule_is_gone(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Forge, "_check_lineage", lambda self, package: None)
    result = judge("name_squat", lab)
    assert result.verdict == BREACHED and "accepted" in result.evidence


def test_http_no_token_is_breached_when_the_guard_lets_everything_in(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolforge.netguard import NetGuard

    monkeypatch.setattr(NetGuard, "evaluate", lambda self, headers: None)
    result = judge("http_no_token", lab)
    assert result.verdict == BREACHED, (result.evidence, details(result))


def test_secret_in_source_is_breached_when_g1_stops_looking(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.verify.g1_static.secretscan.scan", lambda text, path: [])
    result = judge("secret_in_source", lab)
    assert result.verdict == BREACHED and "G1 passed" in result.evidence


def test_secret_in_source_is_breached_when_the_matched_text_is_stored(lab: Lab) -> None:
    """G1 still rejects, but the matched text turns up in a stored gate row: the leak checks must see it."""
    real = catalog.drive_secret_in_source

    def leaking(lab: Lab) -> dict[str, Any]:
        obs = real(lab)
        obs["in_gate_rows"] = True
        return obs

    attack = catalog.Attack(**{**get_attack("secret_in_source").__dict__, "drive": leaking})
    result = run_one(attack, lab, deadline_s=60)
    assert result.verdict == BREACHED and "gate rows" in result.evidence


def test_ledger_tamper_is_breached_when_verification_accepts_an_edit(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.ledger.verify_chain", lambda events: (True, "looks fine"))
    result = judge("ledger_tamper", lab)
    assert result.verdict == BREACHED and "still verified" in result.evidence


def test_ledger_tamper_is_inconclusive_when_the_control_does_not_verify(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "toolforge.ledger.verify_chain", lambda events: (False, "broken before anyone touched it")
    )
    result = judge("ledger_tamper", lab)
    assert result.verdict == INCONCLUSIVE and "control" in result.evidence


def test_approval_over_mcp_is_breached_when_a_promote_tool_exists(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Lab, "mcp_tool_names", lambda self: ["forge_find", "forge_promote"])
    assert judge("approval_over_mcp", lab).verdict == BREACHED


def test_approval_over_mcp_is_breached_when_a_call_reaches_the_runner(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Lab, "runner_calls", lambda self: 1)
    assert judge("approval_over_mcp", lab).verdict == BREACHED


def test_approval_over_mcp_notices_an_operator_import_in_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = tmp_path / "server"
    server.mkdir()
    (server / "__init__.py").write_text("", encoding="utf-8")
    (server / "sneaky.py").write_text(
        "from toolforge.services.operator_actions import promote\n", encoding="utf-8"
    )
    fake = type("FakeServerPackage", (), {"__file__": str(server / "__init__.py")})
    monkeypatch.setattr("toolforge.server.__file__", fake.__file__)
    assert catalog.server_imports_of(("promote", "decide_approval")) == ["sneaky.py:promote"]


# --- The surfaces that came after: browser plans, signed bundles, chains ----------------------------------------------------------


def lenient_plan_validation(plan: Any, **kwargs: Any) -> list[Any]:
    return []


def test_plan_host_escape_is_breached_when_the_plan_validator_accepts_everything(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.adapters.browser_plan.validate_plan", lenient_plan_validation)
    result = judge("plan_host_escape", lab)
    assert result.verdict == BREACHED and "G1 passed" in result.evidence, (result.evidence, details(result))


def test_plan_host_escape_is_breached_when_an_argument_can_move_the_address(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "toolforge.adapters.browser_plan.render_plan",
        lambda plan, args, hosts: [{"step": "goto", "url": "https://evil.invalid/"}],
    )
    result = judge("plan_host_escape", lab)
    assert result.verdict == BREACHED and "moved a goto" in result.evidence, (
        result.evidence,
        details(result),
    )


def test_plan_host_escape_is_breached_when_the_runner_ignores_where_the_page_went(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.adapters.browser_plan._page_url_ok", lambda reply, hosts, index: None)
    result = judge("plan_host_escape", lab)
    assert result.verdict == BREACHED and "went on" in result.evidence, (result.evidence, details(result))


def test_plan_vocabulary_is_breached_when_the_plan_validator_accepts_everything(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.adapters.browser_plan.validate_plan", lenient_plan_validation)
    result = judge("plan_vocabulary", lab)
    assert result.verdict == BREACHED, (result.evidence, details(result))


def test_plan_page_text_is_breached_when_page_text_is_not_cleaned(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.adapters.browser_plan._clean", lambda value, limit: value)
    result = judge("plan_page_text", lab)
    assert result.verdict == BREACHED and "hidden" in result.evidence, (result.evidence, details(result))


class _Accepting:
    signer = type("Signer", (), {"trust": "untrusted"})()


def test_bundle_forgery_is_breached_when_verification_accepts_every_bundle(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.provenance.bundle.verify_bundle", lambda path, settings: _Accepting())
    result = judge("bundle_forgery", lab)
    assert result.verdict == BREACHED and "verified" in result.evidence, (result.evidence, details(result))


def test_bundle_forgery_is_breached_when_checking_a_bundle_writes_to_the_data_directory(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = catalog.bundle.verify_bundle

    def writing(path: Any, settings: Any) -> Any:
        (settings.home / "extracted.txt").write_text("x", encoding="utf-8")
        return real(path, settings)

    monkeypatch.setattr("toolforge.provenance.bundle.verify_bundle", writing)
    result = judge("bundle_forgery", lab)
    assert result.verdict == BREACHED and "changed" in result.evidence, (result.evidence, details(result))


def test_bundle_forgery_is_inconclusive_when_the_untouched_bundle_does_not_verify(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolforge.errors import ProvenanceError

    def refusing(path: Any, settings: Any) -> Any:
        raise ProvenanceError("refused", code="bundle_unreadable")

    monkeypatch.setattr("toolforge.provenance.bundle.verify_bundle", refusing)
    assert judge("bundle_forgery", lab).verdict == INCONCLUSIVE


def test_bundle_stranger_is_breached_when_the_receivers_g1_stops_looking(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("toolforge.verify.g1_static.secretscan.scan", lambda text, path: [])
    result = judge("bundle_stranger", lab)
    assert result.verdict == BREACHED and "G1 passed" in result.evidence, (result.evidence, details(result))


def test_chain_laundering_is_breached_when_chains_accept_any_tool(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolforge.services.chain import ChainRunner

    monkeypatch.setattr(ChainRunner, "_ineligible", lambda self, record: None)
    result = judge("chain_laundering", lab)
    assert result.verdict == BREACHED, (result.evidence, details(result))


def test_chain_ref_abuse_is_breached_when_requests_are_not_checked_first(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolforge.services import chain

    def lenient(raw: Any, *, max_steps: int) -> list[Any]:
        items = raw if isinstance(raw, list) else [raw]
        return [chain.Step(f"s{i}", "chain_echo", {}, 0) for i, _ in enumerate(items)]

    monkeypatch.setattr("toolforge.services.chain.parse_request", lenient)
    result = judge("chain_ref_abuse", lab)
    assert result.verdict == BREACHED and "step ran" in result.evidence, (result.evidence, details(result))


def blind_scan(text: Any) -> dict[str, Any]:
    return {"measured": True, "score": 0, "suspected": False, "findings": []}


def test_poisoned_description_is_breached_when_nothing_flags_it(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1's scan and the response fence both go blind: the description reaches an agent unflagged."""
    monkeypatch.setattr(textsafety, "scan", blind_scan)
    result = judge("poisoned_description", lab)
    assert result.verdict == BREACHED and "without a flag" in result.evidence


def test_poisoned_description_is_inconclusive_when_the_scan_is_not_measured(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        textsafety, "scan", lambda text: {"measured": False, "score": None, "suspected": None, "findings": []}
    )
    result = judge("poisoned_description", lab)
    assert result.verdict == INCONCLUSIVE and "not available" in result.evidence


# --- The attacks that need a sandbox, without one -------------------------------------------------------------------------------


class NoSandbox:
    """A runner whose sandbox never opens: every hostile package is built and submitted, none can run."""

    def open(self, spec: Any) -> Any:
        raise SandboxUnavailable("no sandbox in this test", hint="this test has none")

    def shutdown(self) -> None:
        pass


@pytest.fixture
def blind_lab(tmp_path: Path, minilocker: None) -> Iterator[Lab]:
    home = tmp_path / "home"
    home.mkdir()
    made = Lab(home, runner=NoSandbox(), docker_ok=True)
    yield made
    made.close()


@pytest.mark.parametrize("attack_id", NEEDS_DOCKER)
def test_without_a_working_sandbox_a_sandbox_attack_is_skipped_never_contained(
    attack_id: str, blind_lab: Lab
) -> None:
    result = run_one(get_attack(attack_id), blind_lab, deadline_s=120)
    assert result.verdict in (SKIPPED, INCONCLUSIVE), (result.verdict, result.evidence)
    assert result.verdict != CONTAINED


def test_a_lab_that_says_docker_is_missing_skips_every_sandbox_attack(lab: Lab) -> None:
    for attack_id in NEEDS_DOCKER:
        result = run_one(get_attack(attack_id), lab, deadline_s=10)
        assert result.verdict == SKIPPED and "Docker" in result.evidence, attack_id


G1_PASSES = {
    "vacuous_tests": (lambda: catalog.package("vacuous_tests", catalog.HONEST, tests=catalog.VACUOUS_TESTS)),
    "decoy_reader": (lambda: catalog.package("decoy_reader", catalog.DECOY_READ)),
    "exfil_hidden": (lambda: catalog.package("exfil_hidden", catalog.EXFIL_OBFUSCATED)),
    "quiet_resolver": (lambda: catalog.package("quiet_resolver", catalog.MANIFEST_LIES)),
    "fork_bomber": (lambda: catalog.package("fork_bomber", catalog.FORK_BOMB)),
    "memory_bomber": (lambda: catalog.package("memory_bomber", catalog.MEMORY_BOMB)),
    "cpu_looper": (lambda: catalog.package("cpu_looper", catalog.CPU_LOOP)),
    "disk_filler": (lambda: catalog.package("disk_filler", catalog.DISK_FILL)),
    "output_flooder": (lambda: catalog.package("output_flooder", catalog.OUTPUT_FLOOD)),
    "poison_printer": (lambda: catalog.package("poison_printer", catalog.POISONED_OUTPUT)),
    "audited_counter": (
        lambda: catalog.package("audited_counter", catalog.INJECTION_COMMENT + catalog.HONEST)
    ),
    "sleeper_tool": (lambda: catalog.package("sleeper_tool", catalog.LATE_BETRAYAL)),
}


@pytest.mark.parametrize("name", sorted(G1_PASSES))
def test_hostile_packages_are_valid_and_get_past_the_real_g1(name: str, lab: Lab) -> None:
    """They must reach the gate or limit they target: a package G1 stops for a boring reason proves nothing about that gate."""
    package = G1_PASSES[name]()
    assert package.validate(lab.settings).ok
    sub = lab.submit(package, gates=lab.static_then_seeded())
    assert sub.obs()["gates"]["G1"]["status"] == "passed", sub.obs()["gates"]["G1"]
    assert sub.status in ("verified", "verified_basic")


def test_the_plain_exfiltrator_is_the_one_package_g1_stops(lab: Lab) -> None:
    sub = lab.submit(catalog.package("exfil_plain", catalog.EXFIL_PLAIN))
    gate = sub.obs()["gates"]["G1"]
    assert gate["status"] == "failed" and "permissions.network is empty" in " ".join(gate["hints"])
    assert lab.static_findings(catalog.package("exfil_hidden", catalog.EXFIL_OBFUSCATED)) == ["dynamic_code"]


def test_g1_names_the_requirement_forms_the_sdist_attack_uses(lab: Lab) -> None:
    for requirement in catalog.BAD_REQUIREMENTS:
        sub = lab.submit(catalog.package("bad_requirement", catalog.HONEST, requirements=[requirement]))
        obs = sub.obs()
        assert obs["registered"] is False and obs["status"] == "rejected"
        assert "requirements.invalid" in " ".join(obs["gates"]["G1"]["hints"])


def test_the_source_only_pin_is_a_valid_name_equals_version_requirement(lab: Lab) -> None:
    assert (
        catalog.package("source_only_pin", catalog.HONEST, requirements=[catalog.SOURCE_ONLY])
        .validate(lab.settings)
        .ok
    )


def test_the_permission_creep_patch_is_valid_and_declares_exactly_one_new_host(lab: Lab) -> None:
    v1 = catalog.package("creeping_tool", catalog.HONEST, version="1.0.0")
    patch = catalog.package(
        "creeping_tool",
        catalog.HONEST,
        version="1.0.1",
        parent_version="1.0.0",
        permissions={"network": [catalog.SECOND_DOMAIN], "secrets": [], "inputs": False, "browser": False},
    )
    assert v1.validate(lab.settings).ok and patch.validate(lab.settings).ok
    assert patch.manifest.permissions.network == [catalog.SECOND_DOMAIN]


def test_every_attack_has_a_driver_with_the_lab_as_its_only_argument() -> None:
    import inspect

    for attack_id in attack_ids():
        parameters = inspect.signature(get_attack(attack_id).drive).parameters
        assert list(parameters) == ["lab"], attack_id
