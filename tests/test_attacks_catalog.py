"""The attack catalogue is well formed, safe to import, and the same as the documents that describe it."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from toolforge.attacks import catalog, runner
from toolforge.attacks.catalog import ATTACKS, attack_ids, attacks_markdown, get_attack

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "ATTACKS.md"
GUIDE = ROOT / "docs" / "guide" / "patches" / "P15-attack-lab.md"
PACKAGE = ROOT / "src" / "toolforge" / "attacks"


def test_ids_are_unique_and_well_formed() -> None:
    ids = attack_ids()
    assert len(ids) == len(set(ids))
    assert all(catalog.ATTACK_ID.match(i) for i in ids)


@pytest.mark.parametrize("attack", ATTACKS, ids=attack_ids())
def test_every_entry_says_what_it_is_and_how_it_is_judged(attack: catalog.Attack) -> None:
    assert attack.title.strip() and attack.attack.strip() and attack.defence.strip()
    assert attack.attack.endswith(".") and attack.defence.endswith(".")
    assert callable(attack.drive)
    assert attack.checks, "an attack with no check can never be judged"
    labels = [c.label for c in attack.checks]
    assert len(labels) == len(set(labels)) and all(labels)


def test_get_attack_finds_each_and_refuses_unknown_ids() -> None:
    assert [get_attack(i).id for i in attack_ids()] == list(attack_ids())
    with pytest.raises(KeyError):
        get_attack("no_such_attack")


def test_every_attack_in_the_guide_table_is_in_the_catalogue() -> None:
    """The guide lists twenty attacks; more may be added, none may be dropped."""
    text = GUIDE.read_text(encoding="utf-8")
    in_guide = set(re.findall(r"^\| `([a-z_]+)` \|", text, re.MULTILINE))
    assert len(in_guide) == 20
    assert in_guide <= set(attack_ids()), sorted(in_guide - set(attack_ids()))


def test_the_attacks_document_embeds_the_generated_table() -> None:
    match = re.search(
        r"<!-- attacks:begin -->\n(.*?)\n<!-- attacks:end -->", DOC.read_text(encoding="utf-8"), re.DOTALL
    )
    assert match, "docs/ATTACKS.md must wrap the table in attacks:begin / attacks:end markers"
    assert match.group(1) == attacks_markdown(), (
        "docs/ATTACKS.md is out of date: replace the text between the markers with `toolforge attacks list --markdown`"
    )


def test_the_documented_verdicts_and_limits_are_the_real_ones() -> None:
    text = DOC.read_text(encoding="utf-8")
    for verdict in catalog.VERDICTS:
        assert f"`{verdict}`" in text
    assert f"{runner.DEADLINE_S} seconds" in text
    assert "not about all possible attacks" in text


# --- Safety of the module itself ---------------------------------------------------------------------------------------------

BANNED_IMPORTS = {"subprocess", "ctypes", "pty", "multiprocessing", "pickle", "marshal"}
BANNED_CALLS = {"exec", "eval", "compile", "os.system", "os.popen", "os.fork", "__import__"}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_the_lab_never_runs_hostile_code_itself(path: Path) -> None:
    """Hostile sources are strings. The lab's own code imports no process or native-code library and calls nothing that runs text."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & BANNED_IMPORTS, (path.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in BANNED_IMPORTS, (path.name, node.lineno)
        elif isinstance(node, ast.Call):
            assert _dotted(node.func) not in BANNED_CALLS, (path.name, node.lineno, _dotted(node.func))


def test_the_lab_never_uses_the_stand_in_sandbox() -> None:
    """`tests/local_sandbox.py` runs code on the host with no isolation: only real sandboxes may run an attack."""
    for path in PACKAGE.glob("*.py"):
        assert "LocalRunner" not in path.read_text(encoding="utf-8").replace(
            '("FakeRunner", "LocalRunner")', ""
        )


def test_every_hostile_source_parses_as_python() -> None:
    sources = {
        name: value
        for name, value in vars(catalog).items()
        if name.isupper() and isinstance(value, str) and "def run(args)" in value
    }
    assert len(sources) >= 12
    for name, source in sources.items():
        ast.parse(source, filename=name)


def test_hostile_sources_name_no_real_host_and_no_real_credential() -> None:
    text = (PACKAGE / "catalog.py").read_text(encoding="utf-8")
    for host in set(re.findall(r"https?://([a-z0-9.\-]+)", text)):
        assert host.endswith((".invalid", ".test")) or host in {"{DOMAIN}"}, host
    assert not re.search(r"AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk_live_", text)
    assert catalog.DOMAIN.endswith(".invalid") and catalog.SECOND_DOMAIN.endswith(".test")


def test_the_resource_attacks_stop_on_their_own() -> None:
    """A limit that failed to hold must not take the machine down: every resource attack has a fixed bound far above the limit."""
    assert "< 1000" in catalog.FORK_BOMB and "sleep(20)" in catalog.FORK_BOMB
    assert "range(64)" in catalog.MEMORY_BOMB
    assert "range(512)" in catalog.DISK_FILL or "512" in catalog.DISK_FILL
    assert "range(300)" in catalog.OUTPUT_FLOOD
