"""`toolforge keys`, `export`, `verify-bundle` and `import`, end to end: two homes, the real harness through the stand-in sandbox, and the real
hash-chained ledger. The commands are the only way into S01: no MCP tool reaches them, which the last tests prove from the source."""

from __future__ import annotations

import ast
import base64
import json
import os
from pathlib import Path

import pytest
from local_sandbox import LocalRunner
from provenance_helpers import members_of, tree, write_members

from toolforge import cli_run
from toolforge.cli import main
from toolforge.provenance import keys
from toolforge.registry import Registry, lifecycle
from toolforge.server import tools

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "toolpacks"
SRC = Path(tools.__file__).resolve().parent.parent
pytestmark = [
    pytest.mark.usefixtures("minilocker"),
    pytest.mark.skipif(os.name != "posix", reason="the harness runs in a Linux container"),
]


class Stand:
    def __init__(self) -> None:
        self.runner = LocalRunner()
        self.shutdowns = 0
        self.runner.shutdown = self._shutdown  # type: ignore[attr-defined]

    def _shutdown(self) -> None:
        self.shutdowns += 1


@pytest.fixture
def stand(monkeypatch: pytest.MonkeyPatch):
    s = Stand()
    monkeypatch.setattr(cli_run, "make_sandbox_runner", lambda settings: s.runner)
    yield s
    s.runner.cleanup()


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two data directories and a function that points the commands at one of them."""
    made = {"origin": tmp_path / "origin", "receiver": tmp_path / "receiver"}
    for folder in made.values():
        folder.mkdir()

    def use(name: str) -> Path:
        monkeypatch.setenv("TOOLFORGE_HOME", str(made[name]))
        return made[name]

    return use


class Cli:
    """Runs `main` and keeps everything it printed, so the tests can prove that no secret was in any of it."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys, self.printed = capsys, []

    def __call__(self, *argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        captured = self.capsys.readouterr()
        self.printed += [captured.out, captured.err]
        return code, captured.out, captured.err


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(capsys)


def seed_of(home: Path) -> str:
    return (keys.keys_dir(home) / keys.PRIVATE_NAME).read_text(encoding="utf-8").split()[1]


# --- keys ---------------------------------------------------------------------------------------------------------------------


def test_keys_init_show_and_list(homes, cli: Cli) -> None:
    home = homes("origin")
    code, out, _ = cli("keys", "init")
    assert code == 0 and "fingerprint is" in out and "0600" in out
    fingerprint = keys.own_public_key(home).fingerprint
    assert fingerprint in out
    code, out, _ = cli("keys", "show")
    assert (
        code == 0
        and out.splitlines()[0] == f"fingerprint: {fingerprint}"
        and str(home / "keys" / "signing.pub") in out
    )
    code, out, _ = cli("keys", "list")
    assert code == 0 and f"{fingerprint}  your own key" in out


def test_keys_init_twice_is_an_error_and_the_key_is_unchanged(homes, cli: Cli) -> None:
    home = homes("origin")
    cli("keys", "init")
    before = seed_of(home)
    code, _, err = cli("keys", "init")
    assert code == 1 and "error [key_exists]" in err and "hint:" in err
    assert seed_of(home) == before


def test_keys_show_before_init_says_how_to_create_one(homes, cli: Cli) -> None:
    homes("origin")
    code, _, err = cli("keys", "show")
    assert code == 1 and "error [no_signing_key]" in err and "keys init" in err


def test_keys_list_on_an_empty_home_explains_the_commands(homes, cli: Cli) -> None:
    homes("origin")
    code, out, _ = cli("keys", "list")
    assert code == 0 and "keys init" in out and "keys trust" in out


def test_keys_trust_untrust_and_list(homes, cli: Cli) -> None:
    homes("origin")
    cli("keys", "init")
    origin_public = homes("origin") / "keys" / "signing.pub"
    other = homes("receiver")
    cli("keys", "init")
    fingerprint = keys.own_public_key(other).fingerprint
    homes("origin")
    code, out, _ = cli("keys", "trust", str(other / "keys" / "signing.pub"), "--label", "the receiver")
    assert code == 0 and f"trusting {fingerprint} (the receiver)" in out and "every gate still runs" in out
    code, out, _ = cli("keys", "list")
    assert [line.split()[1] for line in out.splitlines()] == ["your", "trusted"] and "the receiver" in out
    assert origin_public.is_file()
    code, out, _ = cli("keys", "untrust", fingerprint)
    assert code == 0 and "no longer trusting" in out
    code, out, _ = cli("keys", "untrust", fingerprint)
    assert code == 1 and "was not in the trusted list" in out
    code, _, err = cli("keys", "untrust", "nonsense")
    assert code == 1 and "error [bad_fingerprint]" in err


def test_keys_trust_refuses_a_private_key_and_copies_nothing(homes, cli: Cli) -> None:
    home = homes("origin")
    cli("keys", "init")
    code, out, err = cli("keys", "trust", str(home / "keys" / "signing.key"))
    assert code == 1 and "error [key_is_private]" in err and "trusting" not in out
    assert list((home / "keys" / "trusted").glob("*")) == []
    assert seed_of(home) not in out + err


# --- export, verify-bundle, import ----------------------------------------------------------------------------------------------


@pytest.fixture
def exported(stand, homes, cli: Cli, tmp_path: Path):
    """word_count@1.0.0 submitted, verified and exported from the origin home: `(path, origin home)`."""
    home = homes("origin")
    assert cli("keys", "init")[0] == 0
    code, out, err = cli("submit", str(EXAMPLES / "word_count"))
    assert code == 0, out + err
    path = tmp_path / "word_count.tfbundle"
    code, out, err = cli("export", "word_count@1.0.0", "-o", str(path))
    assert code == 0, out + err
    return path, home


def test_export_prints_what_it_wrote_and_how_to_check_it(exported, cli: Cli) -> None:
    path, home = exported
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o644
    printed = cli.printed[-2]
    assert "exported word_count@1.0.0" in printed and f"toolforge verify-bundle {path}" in printed
    assert seed_of(home) not in printed


def test_export_will_not_overwrite_without_force(exported, homes, cli: Cli) -> None:
    path, _ = exported
    homes("origin")
    before = path.read_bytes()
    code, _, err = cli("export", "word_count@1.0.0", "-o", str(path))
    assert code == 1 and "error [output_exists]" in err and "--force" in err and path.read_bytes() == before
    assert cli("export", "word_count@1.0.0", "-o", str(path), "--force")[0] == 0


def test_export_of_an_unknown_version_or_without_a_key_is_an_error(
    exported, homes, cli: Cli, tmp_path: Path
) -> None:
    homes("origin")
    code, _, err = cli("export", "nothing@1.0.0", "-o", str(tmp_path / "n.tfbundle"))
    assert code == 1 and "error [unknown_version]" in err
    homes("receiver")
    code, _, err = cli("export", "word_count@1.0.0", "-o", str(tmp_path / "n.tfbundle"))
    assert code == 1 and "error [unknown_version]" in err and not (tmp_path / "n.tfbundle").exists()


def test_verify_bundle_prints_the_checks_and_who_signed_and_changes_nothing(
    exported, homes, cli: Cli
) -> None:
    path, _ = exported
    receiver = homes("receiver")
    code, out, _ = cli("verify-bundle", str(path))
    assert code == 0 and "unsigned by anyone you trust" in out and "ok: the signature is valid" in out
    assert "nothing was imported or changed" in out and "does not show the tool is safe" in out
    assert tree(receiver) == {}


def test_verify_bundle_json_is_machine_readable(exported, homes, cli: Cli) -> None:
    path, _ = exported
    homes("receiver")
    code, out, _ = cli("verify-bundle", str(path), "--json")
    document = json.loads(out)
    assert code == 0 and document["tool"] == "word_count" and document["signed_by_trusted_key"] is False
    assert document["provenance"]["informational_only"] is True and len(document["checks"]) == 7


def test_verify_bundle_after_trusting_the_signer(exported, homes, cli: Cli) -> None:
    path, origin = exported
    homes("receiver")
    cli("keys", "trust", str(origin / "keys" / "signing.pub"), "--label", "origin machine")
    code, out, _ = cli("verify-bundle", str(path))
    assert code == 0 and "a key you trust" in out and "origin machine" in out


def test_import_registers_the_tool_and_runs_every_local_gate(exported, stand, homes, cli: Cli) -> None:
    path, origin = exported
    receiver = homes("receiver")
    stand.shutdowns = 0
    opened = len(stand.runner.handles)
    code, out, err = cli("import", str(path))
    assert code == 0, out + err
    assert "local verification (the result that counts)" in out and "unsigned by anyone you trust" in out
    assert len(stand.runner.handles) > opened  # gates opened sandboxes here: nothing was taken on trust
    assert stand.shutdowns == 1
    fingerprint = keys.own_public_key(origin).fingerprint
    with Registry(receiver) as registry:
        record = registry.find_version("word_count", "1.0.0")
        assert record.status == lifecycle.VERIFIED_BASIC and record.created_by == f"import:{fingerprint}"
        assert registry.active_version("word_count") is None  # imported, not promoted
        assert len(registry.gate_results(record.id)) == 6


def test_import_json_has_the_bundle_and_the_local_verification_apart(exported, homes, cli: Cli) -> None:
    path, _ = exported
    homes("receiver")
    code, out, _ = cli("import", str(path), "--json")
    document = json.loads(out)
    assert code == 0 and document["already_registered"] is False
    assert document["bundle"]["provenance"]["informational_only"] is True
    assert document["local_verification"]["status"] == "verified_basic"


def test_importing_twice_runs_no_new_sandbox_and_says_so(exported, stand, homes, cli: Cli) -> None:
    path, _ = exported
    homes("receiver")
    cli("import", str(path))
    opened = len(stand.runner.handles)
    code, out, _ = cli("import", str(path))
    assert code == 0 and "already registered" in out and len(stand.runner.handles) == opened


def test_a_bundle_that_was_changed_is_refused_and_registers_nothing(
    exported, homes, cli: Cli, tmp_path: Path
) -> None:
    path, _ = exported
    members = members_of(path)
    blob = bytearray(members["package.tar.gz"])
    blob[len(blob) // 2] ^= 0x01
    broken = write_members(tmp_path / "broken.tfbundle", {**members, "package.tar.gz": bytes(blob)})
    receiver = homes("receiver")
    for command in ("verify-bundle", "import"):
        code, out, err = cli(command, str(broken))
        assert code == 1 and "error [blob_sha_mismatch]" in err and "do not import" in err
    with Registry(receiver) as registry:
        assert registry.list_versions() == []


def test_a_file_that_is_not_a_bundle_is_refused_with_a_hint(
    exported, homes, cli: Cli, tmp_path: Path
) -> None:
    homes("receiver")
    junk = tmp_path / "junk.tfbundle"
    junk.write_text("hello")
    code, _, err = cli("verify-bundle", str(junk))
    assert code == 1 and "error [bundle_unreadable]" in err and "toolforge export" in err
    code, _, err = cli("import", str(tmp_path / "absent.tfbundle"))
    assert code == 1 and "error [bundle_missing]" in err


@pytest.mark.parametrize("value", ["0", "-1", "201"])
def test_import_checks_the_fuzz_input_count_before_doing_anything(
    exported, homes, cli: Cli, value: str
) -> None:
    path, _ = exported
    receiver = homes("receiver")
    code, _, err = cli("import", str(path), "--fuzz-inputs", value)
    assert code == 1 and "error [bad_option]" in err
    with Registry(receiver) as registry:
        assert registry.list_versions() == []


def test_the_private_key_appears_in_nothing_any_command_printed(exported, homes, cli: Cli) -> None:
    path, origin = exported
    homes("origin")
    for argv in (
        ("keys", "show"),
        ("keys", "list"),
        ("verify-bundle", str(path)),
        ("verify-bundle", str(path), "--json"),
    ):
        cli(*argv)
    seed = seed_of(origin)
    raw = base64.b64decode(seed)
    assert not [text for text in cli.printed if seed in text]
    assert seed.encode() not in path.read_bytes() and raw not in path.read_bytes()


# --- No MCP tool reaches any of this --------------------------------------------------------------------------------------------


def _server_modules() -> list[Path]:
    return sorted((SRC / "server").glob("*.py")) + [SRC / "cli_serve.py"]


def test_the_server_never_imports_provenance_or_its_commands() -> None:
    offenders = []
    for path in _server_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            offenders += [f"{path.name}: {n}" for n in names if "provenance" in n]
    assert offenders == []


def test_the_operator_only_list_names_every_s01_action() -> None:
    assert {
        "export_bundle",
        "import_bundle",
        "load_private_key",
        "init_keys",
        "trust_key",
        "untrust_key",
    } <= set(tools.OPERATOR_ONLY)


def test_no_tool_name_offers_any_s01_action() -> None:
    names = " ".join(tool.name for tool in tools.META_TOOLS)
    for word in ("bundle", "export", "import", "attest", "sign", "trust", "key"):
        assert word not in names


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_only_the_key_and_attestation_modules_use_the_cryptography_package() -> None:
    users = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "cryptography" in _imported_roots(path)
    )
    assert users == ["provenance/attest.py", "provenance/keys.py"]
