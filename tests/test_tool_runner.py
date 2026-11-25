"""`ToolRunner` (host side of one call) with a `FakeRunner`: no Docker, no tool code is ever executed.

The harness that runs inside the sandbox is tested in `tests/test_harness.py`; the real containers in `tests/docker/`. Here a
scripted sandbox plays the harness's part by printing envelopes, so every way a sandbox can answer, honestly or not, is covered.
Test names that the runner's docstring promises are kept exactly.
"""

from __future__ import annotations

import ast
import copy
import gzip
import hashlib
import io
import json
import logging
import tarfile
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from toolforge.config import Settings
from toolforge.errors import RegistryError, SandboxError, SandboxUnavailable, ToolNotRunnable
from toolforge.models.package import ToolPackage
from toolforge.models.results import CallResult
from toolforge.registry import Registry, VersionRecord
from toolforge.sandbox import FakeRunner, RunOutput, harness
from toolforge.sandbox.fake import FakeHandle
from toolforge.services import runner as runner_module
from toolforge.services.runner import GRACE_S, Envelope, ToolRunner, parse_envelope, repack_blob

NONCE_LEN = 32


# --- Helpers -------------------------------------------------------------------------------------------------------------


def envelope_bytes(nonce: str, **fields: Any) -> bytes:
    """What the harness would print: the marker, the JSON with `<` and `>` escaped, the end marker."""
    body: dict[str, Any] = {
        "ok": True,
        "result": {"words": 2},
        "error": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "duration_ms": 1,
        "truncated": False,
    }
    body.update(fields)
    text = json.dumps(body).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<<TF:{nonce}>>{text}<<TF-END:{nonce}>>\n".encode()


def error_envelope(nonce: str, kind: str = "ToolError", message: str = "no", **fields: Any) -> bytes:
    error = {"type": kind, "message": message, "trace_tail": fields.pop("trace_tail", "")}
    return envelope_bytes(nonce, ok=False, result=None, error=error, **fields)


def out(
    stdout: bytes = b"",
    *,
    code: int = 0,
    stderr: bytes = b"",
    timed_out: bool = False,
    truncated: bool = False,
):
    return RunOutput(code, stdout, stderr, timed_out, truncated)


def nonce_of(argv: list[str]) -> str:
    return argv[-1]


class LoggingRunner(FakeRunner):
    """A FakeRunner that logs `open`, `close` and every `put`, and can be told to fail one step."""

    def __init__(
        self, log: list[str], script: Any = None, *, fail: str | None = None, close_error: bool = False
    ):
        super().__init__(script)
        self.log = log
        self.fail = fail
        self.close_error = close_error

    def open(self, spec):  # type: ignore[no-untyped-def]
        self.log.append("open")
        handle: FakeHandle = super().open(spec)
        original_close, original_thaw, original_put = handle.close, handle.thaw, handle.put

        def close():  # type: ignore[no-untyped-def]
            self.log.append("close")
            summary = original_close()
            if self.close_error:
                raise SandboxError("could not remove the container /var/run/docker.sock")
            return summary

        def thaw(blob: bytes) -> None:
            if self.fail == "thaw":
                raise SandboxError("thaw failed")
            original_thaw(blob)

        def put(path: str, data: bytes) -> None:
            if self.fail == "put":
                raise SandboxError("put failed")
            original_put(path, data)

        handle.close, handle.thaw, handle.put = close, thaw, put  # type: ignore[method-assign]
        return handle


class RecordingLedger:
    def __init__(self, rig: Rig, path: str | None, task_id: str) -> None:
        self.rig, self.path, self.task_id = rig, path, task_id

    def append(self, actor: str, type: str, payload: dict | None = None) -> dict:
        if self.rig.ledger_fail == type:
            raise OSError("disk full")
        self.rig.log.append(f"ledger:{type}")
        event = {"path": self.path, "task": self.task_id, "actor": actor, "type": type, "payload": payload}
        self.rig.events.append(event)
        return event


class Blobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def get(self, sha: str) -> bytes:
        return self.data[sha]


class StandInRegistry:
    """The part of Registry a call needs, with any manifest a test wants (the real one refuses hostile manifests)."""

    def __init__(self) -> None:
        self.blobs = Blobs()
        self.records: dict[int, VersionRecord] = {}
        self.calls: list[tuple] = []
        self.record_error: Exception | None = None
        self.reads: list[int] = []
        self.transitions: list[tuple] = []
        self.approvals: list[Any] = []  # `Approval`-like rows; P09's permission tests fill it

    def add(self, record: VersionRecord) -> VersionRecord:
        self.records[record.id] = record
        return record

    def get_version(self, version_id: int) -> VersionRecord | None:
        self.reads.append(version_id)
        return self.records.get(version_id)

    def find_version(self, name: str, version: str) -> VersionRecord | None:
        return next((r for r in self.records.values() if (r.name, r.version) == (name, version)), None)

    def active_version(self, name: str) -> VersionRecord | None:
        return next((r for r in self.records.values() if r.name == name and r.status == "active"), None)

    def get_tool(self, name: str) -> Any:
        return next((r for r in self.records.values() if r.name == name), None)

    def list_approvals(self, status: str | None = None) -> list[Any]:
        return [a for a in self.approvals if status is None or a.status == status]

    def transition(
        self, version_id: int, to: str, *, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        record = self.records[version_id]
        if record.status != "active" or to != "quarantined":
            raise RegistryError("illegal transition", code="bad_transition")
        self.records[version_id] = replace(record, status=to)
        self.transitions.append((version_id, to, actor, reason))
        return {
            "type": "forge.quarantined",
            "version_id": version_id,
            "actor": actor,
            "reason": reason,
            "related": [],
        }

    def record_call(self, version_id, ok, duration_ms, error_class=None, session_id=None) -> None:  # type: ignore[no-untyped-def]
        if self.record_error is not None:
            raise self.record_error
        self.calls.append((version_id, ok, duration_ms, error_class, session_id))


class Rig:
    """Everything a test needs: the runner under test, its scripted sandbox, the ledger events, a clock and a registry."""

    def __init__(self, tmp_path: Path, make_payload: Callable[[], dict]) -> None:
        self.tmp_path = tmp_path
        self.make_payload = make_payload
        self.log: list[str] = []
        self.events: list[dict] = []
        self.ledger_fail: str | None = None
        self.now = 100.0
        self.settings = Settings(home=tmp_path / "home")
        self.fake = LoggingRunner(self.log)
        self.registry = StandInRegistry()
        self.runner = ToolRunner(
            self.fake,
            self.registry,
            self.settings,
            lambda path, task: RecordingLedger(self, path, task),
            clock=lambda: self.now,
            new_id=lambda: "call0001",
        )
        self._next_id = 1

    # -- packages ---------------------------------------------------------------------------------------------------------

    def package(self, *, inputs: bool = False, **manifest_changes: Any) -> ToolPackage:
        payload = self.make_payload()
        payload["manifest"]["permissions"]["inputs"] = inputs
        payload["manifest"].update(manifest_changes)
        return ToolPackage.from_submission(payload)

    def register(
        self,
        package: ToolPackage | None = None,
        *,
        status: str = "active",
        blob: bytes | None = None,
        **changes: Any,
    ) -> VersionRecord:
        package = package or self.package()
        data = package.to_tar() if blob is None else blob
        sha = hashlib.sha256(data).hexdigest()
        self.registry.blobs.data[sha] = data
        record = VersionRecord(
            id=self._next_id,
            name=package.manifest.name,
            version=package.manifest.version,
            status=status,
            code_sha=package.code_sha,
            blob_sha=sha,
            manifest=package.manifest.model_dump(mode="json"),
            created_by="test",
            parent_version=None,
            created_at="2026-11-20T00:00:00Z",
            updated_at="2026-11-20T00:00:00Z",
        )
        self._next_id += 1
        return self.registry.add(replace(record, **changes))

    # -- scripting the sandbox ----------------------------------------------------------------------------------------------

    def answers(self, make: Callable[[str], RunOutput] | RunOutput) -> None:
        """The harness's reply: a `RunOutput`, or a function of the nonce."""

        def responder(argv: list[str], files: Mapping[str, bytes]) -> RunOutput:
            return make(nonce_of(argv)) if callable(make) else make

        self.fake.expect(responder)

    def succeeds(self, result: Any = None, **fields: Any) -> None:
        self.answers(
            lambda nonce: out(
                envelope_bytes(nonce, result={"words": 2} if result is None else result, **fields)
            )
        )

    def run(self, name: str = "word_count", args: dict | None = None, **kwargs: Any) -> CallResult:
        return self.runner.run(name, {"text": "a b"} if args is None else args, **kwargs)


@pytest.fixture
def rig(tmp_path: Path, make_payload: Callable[[], dict], monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(
        runner_module, "ARG_CHECK_ISOLATED", False
    )  # in-process schema check; one test keeps the real one
    r = Rig(tmp_path, make_payload)
    r.register()
    r.succeeds()
    return r


def opened_nothing(rig: Rig) -> None:
    assert rig.fake.opened == [] and rig.fake.handles == []
    assert rig.events == []


# --- A successful call, and what the host asked the sandbox for -----------------------------------------------------------


def test_a_successful_call_returns_the_result_and_the_version(rig: Rig) -> None:
    result = rig.run()
    assert result.ok is True
    assert result.result == {"words": 2}
    assert result.error is None
    assert result.version == "word_count@1.0.0"
    assert result.call_id == "call0001"
    assert result.truncated is False and result.sandbox_timed_out is False


def test_the_sandbox_is_asked_for_what_the_manifest_says(rig: Rig) -> None:
    rig.run()
    (spec,) = rig.fake.opened
    assert spec.network == "none"
    assert spec.memory_mb == 128
    assert spec.workspace_mb == runner_module.WORKSPACE_MB
    assert spec.image == rig.settings.runtime_image
    assert spec.decoys == rig.settings.runtime_decoys
    assert spec.label == "call:word_count@1.0.0"
    assert spec.ledger_path == str(rig.settings.home / "ledgers" / "word_count@1.0.0" / "call0001.jsonl")


def test_the_command_and_its_limits(rig: Rig) -> None:
    rig.run()
    (call,) = rig.fake.handles[0].calls
    nonce = call.argv[-1]
    assert call.argv == ["python", "/workspace/.tf/harness.py", nonce]
    assert len(nonce) == NONCE_LEN and int(nonce, 16) >= 0
    assert call.timeout_s == 10 + GRACE_S  # the manifest's timeout plus a grace
    assert call.max_out == 16 * 1024 * runner_module.ESCAPE_FACTOR + runner_module.ENVELOPE_OVERHEAD


def test_every_call_gets_a_fresh_random_nonce(rig: Rig) -> None:
    rig.run()
    rig.run()
    nonces = {h.calls[0].argv[-1] for h in rig.fake.handles}
    assert len(nonces) == 2


def test_the_workspace_is_filled_the_way_the_harness_expects(rig: Rig) -> None:
    rig.run(args={"text": "hello"})
    files = rig.fake.handles[0].files
    assert files[harness.HARNESS_PATH].decode() == harness.HARNESS_SOURCE
    assert (
        files[harness.SDK_PATH] == Path(runner_module.__file__).parent.parent.joinpath("sdk.py").read_bytes()
    )
    assert json.loads(files[harness.REQUEST_PATH]) == {
        "args": {"text": "hello"},
        "entrypoint": "tool.py:run",
        "limits": {"timeout_s": 10, "memory_mb": 128, "output_kb": 16},
    }
    assert "/workspace/pkg/tool.py" in files and "/workspace/pkg/manifest.json" in files
    assert not any(path.startswith("/workspace/inputs/") for path in files)  # no files were attached


def test_limits_fall_back_to_the_settings_defaults_when_the_manifest_has_none(rig: Rig) -> None:
    record = rig.registry.active_version("word_count")
    assert record is not None
    manifest = copy.deepcopy(record.manifest)
    manifest.pop("limits")
    rig.registry.add(replace(record, manifest=manifest))
    rig.run()
    sent = json.loads(rig.fake.handles[0].files[harness.REQUEST_PATH])["limits"]
    assert sent == {
        "timeout_s": rig.settings.default_timeout_s,
        "memory_mb": rig.settings.default_memory_mb,
        "output_kb": harness.DEFAULT_OUTPUT_KB,
    }


def test_attached_files_are_written_under_inputs(rig: Rig) -> None:
    rig.register(rig.package(inputs=True, name="csv_tool"))
    result = rig.run("csv_tool", files={"inputs/data.csv": b"a,b\n1,2\n", "inputs/sub/x.txt": b"x"})
    assert result.ok is True
    files = rig.fake.handles[0].files
    assert files["/workspace/inputs/data.csv"] == b"a,b\n1,2\n"
    assert files["/workspace/inputs/sub/x.txt"] == b"x"


def test_the_runner_never_executes_tool_code(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "ran-on-the-host"
    payload = rig.make_payload()
    payload["manifest"]["name"] = "marker_tool"
    payload["files"]["tool.py"] = (
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n\ndef run(args):\n    return {{'words': 0}}\n"
    )
    rig.register(ToolPackage.from_submission(payload))
    assert rig.run("marker_tool").ok is True  # the scripted sandbox answered; nothing imported the tool
    assert not marker.exists()
    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not called & {"exec", "eval", "compile", "__import__"}
    assert not attributes & {
        "import_module",
        "spec_from_file_location",
        "exec_module",
        "run_path",
        "system",
        "Popen",
    }
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"subprocess", "runpy", "multiprocessing", "os"}


# --- Resolving what to run -------------------------------------------------------------------------------------------------


def test_a_tool_name_runs_its_active_version(rig: Rig) -> None:
    rig.register(rig.package(version="2.0.0"), status="verified")
    assert rig.run("word_count").version == "word_count@1.0.0"


def test_name_at_version_runs_that_version(rig: Rig) -> None:
    rig.register(rig.package(version="2.0.0"), status="deprecated")
    assert rig.run("word_count@2.0.0", allow_status=("deprecated",)).version == "word_count@2.0.0"


def test_a_version_record_is_re_read_so_a_stale_status_does_not_run(rig: Rig) -> None:
    record = rig.registry.active_version("word_count")
    assert record is not None
    rig.registry.add(replace(record, status="quarantined"))  # changed after the caller got its copy
    with pytest.raises(ToolNotRunnable) as info:
        rig.runner.run(record, {"text": "a"})
    assert info.value.code == "status_not_allowed"
    assert rig.registry.reads == [record.id]
    opened_nothing(rig)


@pytest.mark.parametrize(
    "status", ["submitted", "verifying", "verified", "verified_basic", "quarantined", "deprecated"]
)
def test_only_active_versions_run_by_default(rig: Rig, status: str) -> None:
    rig.register(rig.package(name="other_tool"), status=status)
    with pytest.raises(ToolNotRunnable) as info:
        rig.run("other_tool@1.0.0")
    assert info.value.code == "status_not_allowed"
    assert "active" in str(info.value)
    opened_nothing(rig)


def test_the_pipeline_may_ask_for_other_statuses(rig: Rig) -> None:
    rig.register(rig.package(name="other_tool"), status="verifying")
    assert rig.run("other_tool@1.0.0", allow_status=("verifying", "verified")).ok is True


def test_an_unknown_tool_and_an_unknown_version_are_registry_errors(rig: Rig) -> None:
    for ref in ("nothing_here", "word_count@9.9.9", "nothing@1.0.0"):
        with pytest.raises(RegistryError) as info:
            rig.run(ref)
        assert info.value.code == "unknown_version"
    opened_nothing(rig)


def test_a_tool_without_an_active_version_says_so(rig: Rig) -> None:
    rig.register(rig.package(name="waiting_tool"), status="verified")
    with pytest.raises(ToolNotRunnable) as info:
        rig.run("waiting_tool")
    assert info.value.code == "no_active_version"


def test_a_version_without_a_frozen_package_is_not_runnable(rig: Rig) -> None:
    rig.register(rig.package(name="unfrozen"), blob=b"x", blob_sha=None)
    with pytest.raises(ToolNotRunnable) as info:
        rig.run("unfrozen")
    assert info.value.code == "no_blob"


def test_a_session_tool_goes_to_the_session_manager_not_to_a_single_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = rig.registry.active_version("word_count")
    assert record is not None
    manifest = {**record.manifest, "kind": "session"}
    rig.registry.add(replace(record, manifest=manifest))
    seen: list[Any] = []
    monkeypatch.setattr(
        rig.runner.sessions,
        "call",
        lambda target, args, files, session_id, record_stats=True: (
            seen.append((target.label, session_id)) or CallResult(ok=True, version=target.label)
        ),
    )
    rig.run()
    assert seen == [(record.label, None)]
    opened_nothing(rig)  # the single-call path opened no sandbox


def test_a_runner_without_a_registry_cannot_run_by_name() -> None:
    bare = ToolRunner(FakeRunner(), None, Settings(), lambda path, task: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(ToolNotRunnable) as info:
        bare.run("word_count", {})
    assert info.value.code == "no_registry"


def test_a_reference_that_is_neither_a_name_nor_a_record_is_a_type_error(rig: Rig) -> None:
    with pytest.raises(TypeError):
        rig.runner.run(42, {})  # type: ignore[arg-type]


# --- Checking what the caller sent -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {},  # a required property is missing
        {"text": 5},  # wrong type
        {"text": "a", "extra": 1},  # additionalProperties is false
        {"text": float("nan")},  # not JSON
        {"text": "\ud800"},  # an unpaired surrogate is not valid Unicode
        {"text": "x" * (runner_module.MAX_ARGS_BYTES + 1)},  # too large
    ],
)
def test_bad_arguments_are_rejected_without_opening_a_sandbox(rig: Rig, args: dict) -> None:
    result = rig.run(args=args)
    assert result.ok is False and result.error is not None
    assert result.error.type == "InvalidArguments"
    assert result.error.hint
    assert result.duration_ms == 0 and result.version == "word_count@1.0.0"
    opened_nothing(rig)
    assert rig.registry.calls == []  # the caller's mistake is not the tool's failure


def test_arguments_that_are_not_an_object_are_rejected(rig: Rig) -> None:
    for args in (["text"], "text", None, 5):
        result = rig.runner.run("word_count", args)  # type: ignore[arg-type]
        assert result.error is not None and result.error.type == "InvalidArguments"
    opened_nothing(rig)


def test_arguments_nested_too_deeply_are_rejected(rig: Rig) -> None:
    deep: Any = "x"
    for _ in range(100):
        deep = [deep]
    result = rig.run(args={"text": deep})
    assert result.error is not None and result.error.type == "InvalidArguments"
    opened_nothing(rig)


def test_schema_rejection_names_the_property(rig: Rig) -> None:
    result = rig.run(args={"text": 5})
    assert result.error is not None
    assert "do not match the input schema" in result.error.message
    assert "text" in result.error.message


def test_a_hostile_schema_cannot_hang_a_call(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real isolated check, with a short limit: a catastrophic regular expression is stopped, and nothing runs."""
    monkeypatch.setattr(runner_module, "ARG_CHECK_ISOLATED", True)
    monkeypatch.setattr(runner_module, "ARG_CHECK_TIMEOUT_S", 1.0)
    record = rig.registry.active_version("word_count")
    assert record is not None
    manifest = copy.deepcopy(record.manifest)
    manifest["input_schema"] = {
        "type": "object",
        "properties": {"text": {"type": "string", "pattern": "^(a+)+$"}},
        "required": ["text"],
    }
    rig.registry.add(replace(record, manifest=manifest))
    started = time.monotonic()
    result = rig.run(args={"text": "a" * 40 + "b"})
    assert time.monotonic() - started < 30
    assert result.error is not None and result.error.type == "InvalidArguments"
    assert "could not be checked" in result.error.message  # fail closed
    opened_nothing(rig)


def test_the_real_isolated_check_accepts_good_arguments(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "ARG_CHECK_ISOLATED", True)
    assert rig.run().ok is True


@pytest.mark.parametrize(
    ("files", "needle"),
    [
        ({"inputs/../x": b"1"}, ".."),
        ({"inputs/a/../b": b"1"}, ".."),
        ({"/etc/passwd": b"1"}, "absolute"),
        ({"inputs\\x.csv": b"1"}, "backslash"),
        ({"inputs//x.csv": b"1"}, "empty segment"),
        ({"inputs/.hidden": b"1"}, "dot"),
        ({"x.csv": b"1"}, "does not start with inputs/"),
        ({"other/x.csv": b"1"}, "does not start with inputs/"),
        ({"inputs/": b"1"}, "empty segment"),
        ({"inputs/a\x00b": b"1"}, "control"),
        ({"inputs/data.csv": "text, not bytes"}, "must be bytes"),
        ({"inputs/a": b"1", "inputs/a/b": b"2"}, "both a file and a folder"),
    ],
)
def test_unsafe_file_paths_are_rejected_without_opening_a_sandbox(rig: Rig, files: dict, needle: str) -> None:
    rig.register(rig.package(inputs=True, name="csv_tool"))
    result = rig.run("csv_tool", files=files)
    assert result.error is not None and result.error.type == "InvalidFiles"
    assert needle in result.error.message
    opened_nothing(rig)
    assert rig.registry.calls == []


def test_files_are_refused_for_a_tool_that_takes_none(rig: Rig) -> None:
    result = rig.run(files={"inputs/data.csv": b"1"})
    assert result.error is not None and result.error.type == "InvalidFiles"
    assert "takes no files" in result.error.message
    opened_nothing(rig)


def test_an_empty_files_mapping_is_fine_for_a_tool_that_takes_none(rig: Rig) -> None:
    assert rig.run(files={}).ok is True


def test_too_many_files_and_too_many_bytes_are_refused(rig: Rig) -> None:
    rig.register(rig.package(inputs=True, name="csv_tool"))
    many = {f"inputs/f{index}.txt": b"x" for index in range(runner_module.MAX_FILES + 1)}
    big = {
        "inputs/a.bin": b"x" * (runner_module.MAX_FILES_BYTES // 2 + 1),
        "inputs/b.bin": b"x" * (runner_module.MAX_FILES_BYTES // 2 + 1),
    }
    for files in (many, big):
        result = rig.run("csv_tool", files=files)
        assert result.error is not None and result.error.type == "InvalidFiles"
    opened_nothing(rig)


def test_files_that_are_not_a_mapping_are_refused(rig: Rig) -> None:
    rig.register(rig.package(inputs=True, name="csv_tool"))
    result = rig.run("csv_tool", files=[("inputs/a", b"x")])  # type: ignore[arg-type]
    assert result.error is not None and result.error.type == "InvalidFiles"


def test_a_file_path_is_clipped_in_the_message(rig: Rig) -> None:
    rig.register(rig.package(inputs=True, name="csv_tool"))
    result = rig.run("csv_tool", files={"inputs/" + "n" * 5000 + "\x1b[2J": b"1"})
    assert result.error is not None
    assert len(result.error.message) < 300 and "\x1b" not in result.error.message


# --- Envelope parsing --------------------------------------------------------------------------------------------------------


def test_envelope_with_the_wrong_nonce_is_ignored(rig: Rig) -> None:
    rig.answers(lambda nonce: out(envelope_bytes("0" * NONCE_LEN, result="forged")))
    result = rig.run()
    assert result.ok is False and result.error is not None and result.error.type == "NoResult"


def test_envelope_last_start_marker_wins(rig: Rig) -> None:
    rig.answers(
        lambda n: out(envelope_bytes(n, result="first") + b"noise" + envelope_bytes(n, result="last"))
    )
    assert rig.run().result == "last"


def test_envelope_text_around_it_is_discarded(rig: Rig) -> None:
    rig.answers(lambda n: out(b"debug line\n" + envelope_bytes(n, result={"words": 3}) + b"trailing\n"))
    assert rig.run().result == {"words": 3}


def test_envelope_without_an_end_marker_is_no_result(rig: Rig) -> None:
    rig.answers(lambda n: out(f"<<TF:{n}>>".encode() + b'{"ok": true, "result": 1'))
    result = rig.run()
    assert result.error is not None and result.error.type == "NoResult"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"result": 1}',  # ok is missing
        b'{"ok": "yes", "result": 1, "error": null}',  # strict types: a string is not a bool
        b'{"ok": true, "result": 1, "error": {"type": "Crash", "message": "x"}}',  # ok and error disagree
        b'{"ok": false, "result": null, "error": null}',  # ok and error disagree
        b'{"ok": false, "result": null, "error": {"type": "Timeout", "message": "x"}}',  # a host-only type
        b'{"ok": false, "result": null, "error": {"type": "SandboxCrashed", "message": "x"}}',
        b'{"ok": false, "result": null, "error": {"type": "InvalidArguments", "message": "x"}}',
        b'{"ok": true, "result": NaN, "error": null}',
        b'{"ok": true, "result": Infinity, "error": null}',
        b'{"ok": true, "result": 1, "error": null, "stdout_tail": 5}',
        b'\xff\xfe{"ok": true}',
        b"[" * 100000,
    ],
)
def test_envelope_with_a_wrong_shape_is_unusable(rig: Rig, body: bytes) -> None:
    rig.answers(lambda n: out(f"<<TF:{n}>>".encode() + body + f"<<TF-END:{n}>>\n".encode()))
    result = rig.run()
    assert result.ok is False and result.error is not None and result.error.type == "NoResult"


def test_envelope_parse_returns_none_or_a_model(rig: Rig) -> None:
    assert parse_envelope(b"", "n") is None
    assert isinstance(parse_envelope(envelope_bytes("n"), "n"), Envelope)


def test_envelope_error_from_the_tool_is_passed_on_with_a_hint(rig: Rig) -> None:
    for kind in ("ToolError", "Crash", "NotJson", "OutputTooLarge"):
        rig.answers(
            lambda n, kind=kind: out(error_envelope(n, kind, "went wrong", trace_tail="Traceback ..."))
        )
        result = rig.run()
        assert result.ok is False and result.result is None
        assert result.error is not None
        assert (result.error.type, result.error.message, result.error.trace_tail) == (
            kind,
            "went wrong",
            "Traceback ...",
        )
        assert result.error.hint


def test_envelope_text_is_stripped_and_clipped(rig: Rig) -> None:
    nasty = "\x1b[31mred\x1b[0m\x00\u202e" + "y" * 10_000
    rig.answers(
        lambda n: out(
            error_envelope(n, "Crash", nasty, trace_tail=nasty, stdout_tail=nasty, stderr_tail=nasty),
        )
    )
    result = rig.run()
    assert result.error is not None
    for text, limit in (
        (result.error.message, runner_module.MESSAGE_CAP),
        (result.error.trace_tail, runner_module.TEXT_CAP),
        (result.stdout_tail, runner_module.TEXT_CAP),
        (result.stderr_tail, runner_module.TEXT_CAP),
    ):
        assert len(text) <= limit
        assert not any(char in text for char in "\x1b\x00\u202e")


def test_envelope_result_is_size_checked_again_on_the_host(rig: Rig) -> None:
    rig.answers(lambda n: out(envelope_bytes(n, result={"blob": "x" * 40_000})))  # the manifest allows 16 KB
    result = rig.run()
    assert result.ok is False and result.error is not None and result.error.type == "OutputTooLarge"
    assert "limits.output_kb is 16" in result.error.message
    assert result.result is None


def test_envelope_result_of_any_json_shape_is_returned_as_data(rig: Rig) -> None:
    for value in (None, True, 0, 1.5, "text", [1, [2]], {"a": {"b": None}}, "<script>alert(1)</script>"):
        rig.succeeds(result=value) if value is not None else rig.answers(
            lambda n: out(envelope_bytes(n, result=None))
        )
        result = rig.run()
        assert result.ok is True and result.result == value


def test_an_envelope_cannot_make_a_call_succeed_after_a_timeout(rig: Rig) -> None:
    rig.answers(lambda n: out(envelope_bytes(n), timed_out=True))
    result = rig.run()
    assert result.ok is False


# --- Failure mapping ---------------------------------------------------------------------------------------------------------


def test_a_timed_out_run_is_a_timeout_whatever_it_printed(rig: Rig) -> None:
    rig.answers(
        lambda n: out(
            envelope_bytes(n, result="I finished!"), timed_out=True, stderr=b"\x1b[31mslow", truncated=True
        )
    )
    result = rig.run()
    assert result.ok is False and result.result is None
    assert result.error is not None and result.error.type == "Timeout"
    assert "10 seconds" in result.error.message
    assert result.sandbox_timed_out is True and result.truncated is True
    assert result.stderr_tail == "[31mslow"  # the escape character is removed, the text is kept
    _, ok, _, error_class, _ = rig.registry.calls[0]
    assert (ok, error_class) == (False, "Timeout")


def test_a_run_whose_sandbox_failed_is_sandbox_crashed(rig: Rig) -> None:
    def explode(argv: list[str], files: Mapping[str, bytes]) -> RunOutput:
        raise SandboxError("container 3f2a vanished: /var/lib/docker/overlay2/abc")

    rig.fake.expect(explode)
    result = rig.run()
    assert result.error is not None and result.error.type == "SandboxCrashed"
    assert "docker" not in result.error.message.lower() and "/var/" not in result.error.message
    assert result.sandbox_timed_out is False


@pytest.mark.parametrize("code", [137, 139, -9, 255])
def test_a_process_killed_from_outside_is_sandbox_crashed(rig: Rig, code: int) -> None:
    rig.answers(out(b"", code=code))
    result = rig.run()
    assert result.error is not None and result.error.type == "SandboxCrashed"
    assert str(code) in result.error.message
    assert ("memory" in result.error.message) == (code in (137, -9))


def test_no_envelope_and_a_normal_exit_is_no_result_with_a_clean_stderr(rig: Rig) -> None:
    rig.answers(out(b"just prints\n", code=0, stderr=b"Traceback\x1b[2J \x00 harness died"))
    result = rig.run()
    assert result.error is not None and result.error.type == "NoResult"
    assert "exit code 0" in result.error.message
    assert result.stderr_tail == "Traceback[2J  harness died"


def test_output_that_was_cut_before_the_envelope_is_output_too_large(rig: Rig) -> None:
    rig.answers(out(b"x" * 100, truncated=True))
    result = rig.run()
    assert result.error is not None and result.error.type == "OutputTooLarge"
    assert result.truncated is True


def test_a_nonzero_small_exit_code_without_an_envelope_is_no_result(rig: Rig) -> None:
    rig.answers(out(b"", code=2, stderr=b"python: can't open file"))
    result = rig.run()
    assert result.error is not None and result.error.type == "NoResult"
    assert "exit code 2" in result.error.message


def test_a_tool_error_is_reported_as_one(rig: Rig) -> None:
    rig.answers(lambda n: out(error_envelope(n, "ToolError", "the file is not a CSV")))
    result = rig.run()
    assert result.error is not None
    assert (result.ok, result.error.type, result.error.message) == (
        False,
        "ToolError",
        "the file is not a CSV",
    )


def test_stdout_and_stderr_tails_come_back_sanitised(rig: Rig) -> None:
    rig.succeeds(stdout_tail="hello\x1b[1m", stderr_tail="warn\x00ing")
    result = rig.run()
    assert (result.stdout_tail, result.stderr_tail) == ("hello[1m", "warning")


def test_truncated_is_true_when_either_side_says_so(rig: Rig) -> None:
    rig.succeeds(truncated=True)
    assert rig.run().truncated is True
    rig.answers(lambda n: out(envelope_bytes(n), truncated=True))
    assert rig.run().truncated is True


# --- Time --------------------------------------------------------------------------------------------------------------------


def test_duration_is_measured_on_the_host(rig: Rig) -> None:
    def slow(argv: list[str], files: Mapping[str, bytes]) -> RunOutput:
        rig.now += 1.25  # the host's clock moves; the tool claims it took 1 ms
        return out(envelope_bytes(nonce_of(argv), duration_ms=1))

    rig.fake.expect(slow)
    result = rig.run()
    assert result.duration_ms == 1250
    assert rig.registry.calls[0][2] == 1250
    assert [e for e in rig.events if e["type"] == "forge.call.finished"][0]["payload"]["duration_ms"] == 1250


def test_a_clock_that_goes_backwards_gives_zero_not_a_negative(rig: Rig) -> None:
    def rewind(argv: list[str], files: Mapping[str, bytes]) -> RunOutput:
        rig.now -= 5
        return out(envelope_bytes(nonce_of(argv)))

    rig.fake.expect(rewind)
    assert rig.run().duration_ms == 0


# --- Cleanup -----------------------------------------------------------------------------------------------------------------


def scenarios() -> list[tuple[str, Callable[[Rig], None], bool]]:
    """(name, how to set the rig up, whether `run` raises)."""

    def ok(rig: Rig) -> None:
        rig.succeeds()

    def tool_error(rig: Rig) -> None:
        rig.answers(lambda n: out(error_envelope(n)))

    def timeout(rig: Rig) -> None:
        rig.answers(out(b"", timed_out=True))

    def crashed(rig: Rig) -> None:
        rig.answers(out(b"", code=137))

    def run_raises(rig: Rig) -> None:
        def boom(argv: list[str], files: Mapping[str, bytes]) -> RunOutput:
            raise SandboxError("gone")

        rig.fake.expect(boom)

    def unexpected_bug(rig: Rig) -> None:
        def boom(argv: list[str], files: Mapping[str, bytes]) -> RunOutput:
            raise RuntimeError("a bug in a stand-in")

        rig.fake.expect(boom)

    def thaw_fails(rig: Rig) -> None:
        rig.fake.fail = "thaw"

    def put_fails(rig: Rig) -> None:
        rig.fake.fail = "put"

    def close_fails(rig: Rig) -> None:
        rig.fake.close_error = True
        rig.succeeds()

    return [
        ("ok", ok, False),
        ("tool_error", tool_error, False),
        ("timeout", timeout, False),
        ("crashed", crashed, False),
        ("run_raises", run_raises, False),
        ("unexpected_bug", unexpected_bug, True),
        ("thaw_fails", thaw_fails, True),
        ("put_fails", put_fails, True),
        ("close_fails", close_fails, False),
    ]


@pytest.mark.parametrize(("name", "setup", "raises"), scenarios(), ids=[s[0] for s in scenarios()])
def test_the_sandbox_is_closed_on_every_path(
    rig: Rig, name: str, setup: Callable[[Rig], None], raises: bool
) -> None:
    setup(rig)
    if raises:
        with pytest.raises(Exception):  # noqa: B017 - the kind differs per scenario; closing is what is under test
            rig.run()
    else:
        rig.run()
    assert len(rig.fake.handles) == 1
    assert rig.log.count("close") == 1  # closed exactly once, whatever happened
    assert rig.log.index("close") > rig.log.index("open")
    assert rig.events[-1]["type"] == "forge.call.finished"  # and the call still has its closing record


def test_a_sandbox_that_could_not_be_closed_is_reported_in_the_ledger_not_to_the_caller(rig: Rig) -> None:
    rig.fake.close_error = True
    rig.succeeds()
    result = rig.run()
    assert result.ok is True
    finished = rig.events[-1]["payload"]
    assert finished["cleanup_ok"] is False
    assert "egress_attempts" not in finished


def test_infrastructure_failures_are_exceptions_for_the_operator_not_results(rig: Rig) -> None:
    rig.fake.available = False
    with pytest.raises(SandboxUnavailable):
        rig.run()
    assert (
        rig.events[-1]["payload"]["error_type"] == "SandboxUnavailable"
    )  # a closing record exists, with no detail
    assert rig.registry.calls == []


def test_an_audit_of_the_sandbox_is_summarised_in_the_finished_event(rig: Rig) -> None:
    rig.succeeds()
    rig.run()
    finished = rig.events[-1]["payload"]
    assert finished["cleanup_ok"] is True
    assert finished["egress_attempts"] == 0 and finished["decoy_events"] == 0


# --- The ledger --------------------------------------------------------------------------------------------------------------


def test_ledger_events_surround_the_sandbox(rig: Rig) -> None:
    rig.run()
    assert rig.log == ["ledger:forge.call.started", "open", "close", "ledger:forge.call.finished"]
    started, finished = rig.events
    assert started["path"] == finished["path"] == rig.fake.opened[0].ledger_path
    assert started["task"] == finished["task"] == "call0001"
    assert started["actor"] == "forge"
    assert started["payload"]["tool"] == "word_count@1.0.0"
    assert started["payload"]["network"] == "none"
    assert started["payload"]["unverified"] is False
    assert started["payload"]["limits"] == {"timeout_s": 10, "memory_mb": 128, "output_kb": 16}
    assert finished["payload"]["ok"] is True and finished["payload"]["error_type"] is None
    assert finished["payload"]["result_bytes"] == len(json.dumps({"words": 2}))


def test_a_call_that_cannot_be_recorded_does_not_run(rig: Rig) -> None:
    rig.ledger_fail = "forge.call.started"
    with pytest.raises(OSError):
        rig.run()
    opened_nothing(rig)
    assert rig.registry.calls == []


def test_a_closing_record_that_cannot_be_written_is_logged_and_the_result_is_kept(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.ledger_fail = "forge.call.finished"
    with caplog.at_level(logging.ERROR, logger="toolforge.services.runner"):
        result = rig.run()
    assert result.ok is True
    assert "ledger event could not be written" in caplog.text


def test_no_argument_value_reaches_the_ledger(rig: Rig) -> None:
    rig.register(rig.package(inputs=True, name="csv_tool"))
    secret_arg, secret_file, secret_result = "ARG-SECRET-4711", "FILE-SECRET-4712", "RESULT-SECRET-4713"
    rig.succeeds(result={"words": secret_result}, stdout_tail="TAIL-SECRET-4714")
    args = {"text": secret_arg}
    rig.run(
        "csv_tool", args, files={"inputs/FILENAME-SECRET-4715.csv": secret_file.encode()}, session_id="sess-1"
    )
    text = json.dumps(rig.events)
    for secret in (secret_arg, secret_file, secret_result, "TAIL-SECRET-4714", "FILENAME-SECRET-4715"):
        assert secret not in text
    started = rig.events[0]["payload"]
    assert started["args_sha256"] == hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
    assert started["args_bytes"] == len(json.dumps(args, sort_keys=True).encode())
    assert (started["files"], started["file_bytes"]) == (1, len(secret_file))
    assert started["session_id"] == "sess-1"


def test_a_session_id_is_recorded_clipped_and_cleaned(rig: Rig) -> None:
    rig.run(session_id="s\x1b[2J" + "x" * 500)
    session = rig.events[0]["payload"]["session_id"]
    assert len(session) <= 80 and "\x1b" not in session
    assert rig.registry.calls[0][4] == session


def test_refused_calls_write_no_ledger_events(rig: Rig) -> None:
    rig.run(args={"text": 1})
    assert rig.events == []


# --- Statistics --------------------------------------------------------------------------------------------------------------


def test_a_successful_call_is_recorded_with_the_host_measured_time(rig: Rig) -> None:
    rig.run()
    assert rig.registry.calls == [(1, True, 0, None, None)]


@pytest.mark.parametrize(
    ("kind", "counts_as_success"),
    [("ToolError", True), ("Crash", False), ("NotJson", False), ("OutputTooLarge", False)],
)
def test_only_a_tool_error_is_not_held_against_the_tool(rig: Rig, kind: str, counts_as_success: bool) -> None:
    rig.answers(lambda n: out(error_envelope(n, kind)))
    result = rig.run()
    assert result.ok is False  # the caller always sees the failure
    version_id, ok, _, error_class, _ = rig.registry.calls[0]
    assert (ok, error_class) == (counts_as_success, kind)


@pytest.mark.parametrize(
    ("response", "error_class"),
    [
        (out(b"", timed_out=True), "Timeout"),
        (out(b"", code=137), "SandboxCrashed"),
        (out(b"", code=0), "NoResult"),
    ],
)
def test_host_side_failures_count_against_the_tool(rig: Rig, response: RunOutput, error_class: str) -> None:
    rig.answers(response)
    rig.run()
    _, ok, _, recorded_class, _ = rig.registry.calls[0]
    assert (ok, recorded_class) == (False, error_class)


def test_a_statistics_failure_does_not_turn_a_finished_call_into_an_exception(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.registry.record_error = RuntimeError("database is locked")
    with caplog.at_level(logging.ERROR, logger="toolforge.services.runner"):
        result = rig.run()
    assert result.ok is True
    assert "statistics could not be recorded" in caplog.text


def test_statistics_reach_the_real_registry(
    tmp_path: Path,
    make_package: Callable[..., ToolPackage],
    registry: Registry,
    activate: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "ARG_CHECK_ISOLATED", False)
    package = make_package()
    record = registry.add_version(package, created_by="test")
    registry.attach_blob(record.id, registry.blobs.put(package.to_tar()))
    activate(record)
    log: list[str] = []
    fake = LoggingRunner(log, lambda argv, files: out(envelope_bytes(nonce_of(argv), result={"words": 2})))
    holder = Rig.__new__(Rig)  # RecordingLedger only needs somewhere to put its log and events
    holder.log, holder.events, holder.ledger_fail = [], [], None
    runner = ToolRunner(
        fake,
        registry,
        Settings(home=tmp_path / "home"),
        lambda path, task: RecordingLedger(holder, path, task),
    )
    for _ in range(3):
        assert runner.run("word_count", {"text": "a b"}).ok is True
    assert runner.run("word_count", {"text": 5}).ok is False  # refused: not counted
    assert registry.stats(record.id).uses == 3
    assert registry.stats("word_count").uses == 3


# --- Unpacking a blob ---------------------------------------------------------------------------------------------------------


def tar_bytes(entries: list[tuple[str, bytes | None, str]], *, gz: bool = True) -> bytes:
    """A tar with the given (name, data, kind) entries; kind is file, dir, symlink, hardlink, device or fifo."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz" if gz else "w") as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data or b"")
                archive.addfile(info, io.BytesIO(data or b""))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                archive.addfile(info)
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, "tool.py"
                archive.addfile(info)
            elif kind == "device":
                info.type = tarfile.CHRTYPE
                archive.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
    return buffer.getvalue()


MANIFEST = json.dumps({"name": "x"}).encode()
GOOD = [("manifest.json", MANIFEST, "file"), ("tool.py", b"def run(a): return 1\n", "file")]


@pytest.mark.parametrize(
    "blob",
    [
        pytest.param(b"definitely not an archive", id="not-an-archive"),
        pytest.param(b"", id="empty"),
        pytest.param(gzip.compress(b"x" * 100), id="gzip-of-garbage"),
        pytest.param(tar_bytes([*GOOD, ("link", None, "symlink")]), id="symlink"),
        pytest.param(tar_bytes([*GOOD, ("hard", None, "hardlink")]), id="hardlink"),
        pytest.param(tar_bytes([*GOOD, ("dev", None, "device")]), id="device"),
        pytest.param(tar_bytes([*GOOD, ("pipe", None, "fifo")]), id="fifo"),
        pytest.param(tar_bytes([*GOOD, ("/etc/cron.d/x", b"x", "file")]), id="absolute-name"),
        pytest.param(tar_bytes([*GOOD, ("../evil.py", b"x", "file")]), id="dotdot-name"),
        pytest.param(tar_bytes([*GOOD, ("a/../../evil.py", b"x", "file")]), id="nested-dotdot-name"),
        pytest.param(tar_bytes([*GOOD, ("..", b"x", "file")]), id="dotdot-only"),
        pytest.param(tar_bytes([("tool.py", b"x", "file")]), id="no-manifest"),
        pytest.param(
            tar_bytes([("sub/manifest.json", MANIFEST, "file"), ("tool.py", b"x", "file")]),
            id="manifest-not-at-top",
        ),
    ],
)
def test_unusable_blobs_are_refused(rig: Rig, blob: bytes) -> None:
    rig.register(rig.package(name="odd_blob"), blob=blob)
    with pytest.raises(ToolNotRunnable) as info:
        rig.run("odd_blob")
    assert info.value.code == "blob_unusable"
    assert "cannot be used" in str(info.value)
    opened_nothing(rig)  # nothing opened, nothing recorded: this is the operator's problem


def test_a_blob_with_too_many_entries_or_too_many_bytes_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "MAX_BLOB_ENTRIES", 3)
    many = tar_bytes([*GOOD, ("a.py", b"1", "file"), ("b.py", b"1", "file")])
    rig.register(rig.package(name="odd_blob"), blob=many)
    with pytest.raises(ToolNotRunnable, match="more than 3 entries"):
        rig.run("odd_blob")
    monkeypatch.setattr(runner_module, "MAX_BLOB_ENTRIES", 5000)
    monkeypatch.setattr(runner_module, "MAX_UNPACKED_BYTES", 50)
    rig.register(
        rig.package(name="odd_blob", version="1.0.1"), blob=tar_bytes([*GOOD, ("big.bin", b"x" * 60, "file")])
    )
    with pytest.raises(ToolNotRunnable, match="unpacks to more than"):
        rig.run("odd_blob@1.0.1")
    opened_nothing(rig)


def test_an_entrypoint_that_is_not_in_the_blob_is_refused(rig: Rig) -> None:
    rig.register(
        rig.package(name="odd_blob"),
        blob=tar_bytes([("manifest.json", MANIFEST, "file"), ("other.py", b"x", "file")]),
    )
    with pytest.raises(ToolNotRunnable, match="entrypoint file"):
        rig.run("odd_blob")
    opened_nothing(rig)


def extract(packed: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}  # type: ignore[union-attr]


def test_a_package_archive_is_placed_under_pkg(rig: Rig) -> None:
    package = rig.package()
    packed, names = repack_blob(package.to_tar())
    placed = extract(packed)
    assert set(placed) == {f"pkg/{name}" for name in names}
    assert "pkg/manifest.json" in placed and "pkg/tool.py" in placed
    assert placed["pkg/tool.py"] == package.files["tool.py"].encode()
    assert "tool.py" in names and "manifest.json" in names  # names are relative to the package


def test_a_frozen_workspace_is_accepted_and_keeps_vendored_dependencies(rig: Rig) -> None:
    blob = tar_bytes(
        [
            ("pkg/manifest.json", MANIFEST, "file"),
            ("pkg/tool.py", b"x", "file"),
            ("pkg/.deps/lib/mod.py", b"y", "file"),
            ("tmp/stray.txt", b"z", "file"),  # not part of the package: dropped
        ],
        gz=False,
    )
    packed, names = repack_blob(blob)
    assert set(extract(packed)) == {"pkg/manifest.json", "pkg/tool.py", "pkg/.deps/lib/mod.py"}
    assert names == [".deps/lib/mod.py", "manifest.json", "tool.py"]


def test_repacking_is_deterministic_and_plain(rig: Rig) -> None:
    blob = rig.package().to_tar()
    assert repack_blob(blob) == repack_blob(blob)
    packed, _ = repack_blob(blob)
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
        for member in archive.getmembers():
            assert member.isreg() and (member.uid, member.gid, member.mtime) == (0, 0, 0)
            assert member.mode == 0o644 and not member.name.startswith(("/", ".."))


def test_directories_in_a_blob_are_skipped_not_refused(rig: Rig) -> None:
    packed, names = repack_blob(tar_bytes([("sub", None, "dir"), *GOOD]))
    assert names == ["manifest.json", "tool.py"] and "pkg/manifest.json" in extract(packed)


# --- Packages that were never registered ------------------------------------------------------------------------------------


def test_run_unverified_runs_a_package_and_records_nothing_in_the_registry(rig: Rig) -> None:
    result = rig.runner.run_unverified(rig.package(), {"text": "a b"})
    assert result.ok is True and result.version == "word_count@1.0.0"
    assert rig.registry.calls == []
    assert rig.events[0]["payload"]["unverified"] is True
    assert rig.fake.opened[0].ledger_path == str(
        rig.settings.home / "ledgers" / "_dev" / "word_count@1.0.0" / "call0001.jsonl"
    )
    assert rig.log == ["ledger:forge.call.started", "open", "close", "ledger:forge.call.finished"]


def test_run_unverified_works_without_a_registry(rig: Rig) -> None:
    bare = ToolRunner(rig.fake, None, rig.settings, lambda path, task: RecordingLedger(rig, path, task))
    assert bare.run_unverified(rig.package(), {"text": "a"}).ok is True


def test_run_unverified_refuses_an_invalid_package(rig: Rig) -> None:
    payload = rig.make_payload()
    payload["manifest"]["entrypoint"] = "missing.py:run"
    with pytest.raises(ToolNotRunnable) as info:
        rig.runner.run_unverified(ToolPackage.from_submission(payload), {})
    assert info.value.code == "package_invalid"
    opened_nothing(rig)


def test_run_unverified_checks_arguments_and_files_like_any_call(rig: Rig) -> None:
    assert rig.runner.run_unverified(rig.package(), {"nope": 1}).error.type == "InvalidArguments"  # type: ignore[union-attr]
    assert (
        rig.runner.run_unverified(rig.package(), {"text": "a"}, files={"inputs/a": b"1"}).error.type
        == "InvalidFiles"
    )  # type: ignore[union-attr]
    opened_nothing(rig)


def test_run_unverified_refuses_a_session_tool(rig: Rig) -> None:
    payload = rig.make_payload()
    payload["manifest"]["kind"] = "session"
    package = ToolPackage.from_submission(payload)
    if not package.validate(rig.settings).ok:
        pytest.skip("a session manifest is not valid until P13")
    with pytest.raises(ToolNotRunnable):
        rig.runner.run_unverified(package, {"text": "a"})


# --- The tripwire: a decoy touched during a real call (P08) --------------------------------------------------------------

DECOY_READ = ("canary.read", {"item": "c1", "label": "AWS access key", "path": "/home/tool/.aws/credentials"})


def operator_events(rig: Rig) -> list[dict]:
    return [e for e in rig.events if str(e["path"]).endswith("operator.jsonl")]


def test_a_decoy_touched_during_a_call_fails_it_and_quarantines_the_version(rig: Rig) -> None:
    rig.fake._events = [("canary.seeded", {"items": [{"id": "c1", "label": "AWS access key"}]}), DECOY_READ]
    result = rig.run()
    assert result.ok is False and result.error is not None
    assert result.error.type == "PolicyViolation"
    assert result.result is None
    assert result.stdout_tail == "" and result.stderr_tail == ""
    assert (
        "/home/tool" not in result.error.message + result.error.hint
    )  # no path from the sandbox is repeated
    assert rig.registry.transitions == [(1, "quarantined", "forge", rig.registry.transitions[0][3])]
    assert "call0001" in rig.registry.transitions[0][3]
    assert rig.registry.records[1].status == "quarantined"
    finished = next(e for e in rig.events if e["type"] == "forge.call.finished")["payload"]
    assert finished["ok"] is False and finished["error_type"] == "PolicyViolation"
    assert finished["decoy_violations"] == 1 and finished["quarantined"] is True
    assert "violations" not in finished  # the raw events are not copied into the call ledger


def test_the_quarantine_is_written_to_the_operators_ledger(rig: Rig) -> None:
    rig.fake._events = [DECOY_READ]
    rig.run()
    (event,) = operator_events(rig)
    assert event["path"] == str(rig.settings.home / "ledgers" / "word_count@1.0.0" / "operator.jsonl")
    assert event["actor"] == "forge" and event["type"] == "forge.quarantined"
    assert event["payload"]["call_id"] == "call0001"
    assert event["payload"]["decoys"] == ["AWS access key"]
    assert "related" not in event["payload"] and "type" not in event["payload"]


def test_a_clean_call_is_not_quarantined_even_with_decoys_planted(rig: Rig) -> None:
    rig.fake._events = [("canary.seeded", {"items": [{"id": "c1", "label": "AWS access key"}]})]
    result = rig.run()
    assert result.ok is True
    assert rig.registry.transitions == [] and operator_events(rig) == []


def test_an_operator_who_turned_decoys_off_gets_no_tripwire(rig: Rig) -> None:
    rig.runner._settings = rig.settings.model_copy(update={"runtime_decoys": False})
    rig.fake._events = [DECOY_READ]
    result = rig.run()
    assert result.ok is True
    assert rig.registry.transitions == []
    assert "violations" not in rig.events[-1]["payload"]


def test_an_unverified_run_is_refused_but_nothing_is_transitioned(rig: Rig) -> None:
    package = rig.package()
    rig.fake._events = [DECOY_READ]
    result = rig.runner.run_unverified(package, {"text": "a b"})
    assert result.ok is False and result.error is not None and result.error.type == "PolicyViolation"
    assert rig.registry.transitions == []
    assert operator_events(rig) == []


def test_a_failed_transition_still_refuses_the_call(rig: Rig) -> None:
    def refuse(*args: Any, **kwargs: Any) -> dict:
        raise RegistryError("no", code="bad_transition")

    rig.registry.transition = refuse  # type: ignore[method-assign]
    rig.fake._events = [DECOY_READ]
    result = rig.run()
    assert result.ok is False and result.error is not None and result.error.type == "PolicyViolation"
    assert rig.events[-1]["payload"]["quarantined"] is False
    assert operator_events(rig) == []


def test_losing_the_operator_ledger_line_does_not_undo_the_quarantine(rig: Rig) -> None:
    rig.ledger_fail = "forge.quarantined"
    rig.fake._events = [DECOY_READ]
    result = rig.run()
    assert result.ok is False and result.error is not None and result.error.type == "PolicyViolation"
    assert rig.registry.records[1].status == "quarantined"


def test_a_hostile_decoy_label_is_cleaned_before_it_reaches_a_ledger(rig: Rig) -> None:
    rig.fake._events = [("canary.read", {"item": "c1", "label": "key\x1b[31m‮" + "A" * 200})]
    rig.run()
    (event,) = operator_events(rig)
    (name,) = event["payload"]["decoys"]
    assert "\x1b" not in name and "‮" not in name and len(name) <= 60
