"""The harness contract, host side: where its files live in a sandbox, how its envelope is marked, and its source.

`HARNESS_SOURCE` is the script that runs inside the sandbox (docs/guide/package-format.md, "How a tool runs"). It is kept here as a
string so the host never reads a file to send it, and a second copy lives in `sandbox/assets/harness.py` so it can be linted, run
and read as Python; `tests/test_harness.py::test_the_asset_copy_equals_the_constant` fails when they differ. Edit the asset, then
paste it here between the quotes (the script contains no triple single quote, a test checks that too).

Importing this module runs nothing from the harness and needs neither MiniLocker nor Docker.
"""

from __future__ import annotations

WORKSPACE = "/workspace"
INPUTS_PREFIX = "inputs/"  # caller files are named inputs/<name> and are written to /workspace/inputs/<name>
HARNESS_PATH = f"{WORKSPACE}/.tf/harness.py"
SDK_PATH = f"{WORKSPACE}/.tf/toolforge_sdk.py"
REQUEST_PATH = f"{WORKSPACE}/.tf/in.json"

TAIL_CHARS = 2000  # how much of stdout, stderr, a message and a traceback an envelope carries
DEFAULT_OUTPUT_KB = 64  # the result size limit when a manifest does not set `limits.output_kb`
# What the harness itself may report. The host-only types (Timeout, SandboxCrashed, NoResult, InvalidArguments, InvalidFiles) are
# never accepted from an envelope: only the host decides those.
ENVELOPE_ERROR_TYPES = ("ToolError", "Crash", "OutputTooLarge", "NotJson")


def start_marker(nonce: str) -> bytes:
    return f"<<TF:{nonce}>>".encode("ascii")


def end_marker(nonce: str) -> bytes:
    return f"<<TF-END:{nonce}>>".encode("ascii")


HARNESS_SOURCE = r'''"""ToolForge harness: runs ONE tool call inside a sandbox and prints ONE result envelope. Standard library only.

Usage: `python /workspace/.tf/harness.py <nonce>`. The host (toolforge.services.runner) has put these next to it before the call:

    <workspace>/pkg/            the unpacked tool package (and pkg/.deps when it has requirements)
    <workspace>/inputs/         files the caller provided for this call
    <workspace>/.tf/in.json     {"args": {...}, "entrypoint": "tool.py:run", "limits": {...}}
    <workspace>/.tf/toolforge_sdk.py

The result is one line on the real stdout: `<<TF:{nonce}>>{json}<<TF-END:{nonce}>>`. The tool's own prints are captured into short
tails inside the envelope and are never mixed into it. The tool runs in this process, so it can forge an envelope; that only lets
it report a different result for itself, which it could return honestly, so the host treats the envelope as untrusted data and
checks it. Nothing here is a security boundary: the container is.

The contract is documented in docs/guide/package-format.md ("How a tool runs"). Keep this file dependency free.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
import traceback

# The harness's own imports must be loaded before the package's folder is put on sys.path, so a package that ships a file
# called json.py or tokenize.py cannot replace what the harness (or traceback) needs. These two are only imported lazily.
for _name in ("linecache", "tokenize", "encodings.utf_8", "encodings.ascii"):
    __import__(_name)

ERROR_TYPES = ("ToolError", "Crash", "OutputTooLarge", "NotJson")
TAIL_CHARS = 2000  # how much of stdout, stderr, a message and a traceback the envelope carries
KEEP_CHARS = (
    4 * TAIL_CHARS
)  # how much of each stream is held while the tool runs, so a chatty tool cannot use up memory
DEFAULT_OUTPUT_KB = 64  # used when the request carries no output limit
FSIZE_BYTES = 32 * 1024 * 1024  # no single file the tool writes may be larger than this
CPU_BACKSTOP_S = 10  # CPU seconds beyond the wall-clock timeout; the host's timeout always fires first, this is a second net


class _ByteSink:
    """`sys.stdout.buffer` for the captured streams: bytes are decoded and kept like text."""

    def __init__(self, tail: _Tail) -> None:
        self._tail = tail

    def write(self, data: bytes) -> int:
        raw = bytes(data)
        self._tail.write(raw.decode("utf-8", "replace"))
        return len(raw)

    def flush(self) -> None:
        pass

    def writable(self) -> bool:
        return True


class _Tail(io.TextIOBase):
    """A write-only text stream that keeps only the end of what is written, so memory stays bounded however much a tool prints."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._held = 0
        self.total = 0  # characters written, including the ones that were dropped
        self.buffer = _ByteSink(self)

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        size = len(text)
        self.total += size
        self._chunks.append(text[-KEEP_CHARS:])
        self._held += min(size, KEEP_CHARS)
        if self._held > 2 * KEEP_CHARS:
            kept = "".join(self._chunks)[-KEEP_CHARS:]
            self._chunks, self._held = [kept], len(kept)
        return size

    def flush(self) -> None:
        pass

    def tail(self) -> str:
        return "".join(self._chunks)[-TAIL_CHARS:]


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except BaseException:
        return f"<{type(value).__name__}>"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 8] + "...[cut]"


def _error(kind: str, message: str, trace: str = "") -> dict:
    assert kind in ERROR_TYPES
    return {"type": kind, "message": _clip(message, TAIL_CHARS), "trace_tail": trace[-TAIL_CHARS:]}


def _trace(exc: BaseException) -> str:
    """The traceback without the harness's own frame, which only says that the harness called the tool."""
    try:
        inner = exc.__traceback__.tb_next if exc.__traceback__ is not None else None
        return "".join(traceback.format_exception(type(exc), exc, inner))
    except BaseException:
        return f"{type(exc).__name__}: {_safe_str(exc)}"


def _crash(exc: BaseException) -> dict:
    return _error("Crash", f"{type(exc).__name__}: {_safe_str(exc)}", _trace(exc))


def _reexec_with_fixed_hash_seed() -> None:
    """Run again with PYTHONHASHSEED=0, because the hash seed cannot be changed once Python is running.

    Without it the order of a set of strings differs between runs, so a tool that returns `list(some_set)` would answer
    differently every time and two runs could not be compared. Skipped where exec is not available; the result is then
    merely less repeatable.
    """
    if os.name != "posix" or os.environ.get("PYTHONHASHSEED") == "0" or not sys.executable:
        return
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        os.execv(sys.executable, [sys.executable, "-B", os.path.abspath(__file__), *sys.argv[1:]])
    except OSError:
        pass


def _apply_limits(limits: dict) -> None:
    """A second net under the host's timeout, and a cap on one file's size. Never memory: Docker's own limit owns that."""
    try:
        import resource
    except ImportError:
        return
    wanted = [(resource.RLIMIT_FSIZE, FSIZE_BYTES)]
    timeout = limits.get("timeout_s")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        wanted.append((resource.RLIMIT_CPU, timeout + CPU_BACKSTOP_S))
    for which, value in wanted:
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass  # the container still enforces its own limits


def _load_entry(package: str, entrypoint: str):
    """Import the entrypoint's file and return the function, or raise. `tool.py:run` -> function `run` of file pkg/tool.py."""
    path, _, function = entrypoint.rpartition(":")
    parts = path.split("/")
    if not path.endswith(".py") or path.startswith("/") or ".." in parts or not function:
        raise ValueError(f"the entrypoint {entrypoint!r} is not '<relative .py path>:<function>'")
    name = path[: -len(".py")].replace("/", ".")
    if name in sys.modules or name.split(".")[0] in sys.stdlib_module_names:
        name = "__tf_entry__"  # never replace a module that is already loaded
    spec = importlib.util.spec_from_file_location(name, os.path.join(package, path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return getattr(module, function)


def _check_result(result: object, limit_kb: int) -> dict | None:
    """None when `result` can go in the envelope, else the error that says why not."""
    try:
        size = len(json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8"))
    except RecursionError:
        return _error("NotJson", "the result is nested too deeply to be written as JSON")
    except (TypeError, ValueError) as exc:  # includes cycles, NaN and text that is not valid Unicode
        return _error(
            "NotJson",
            f"the result cannot be written as JSON: {_safe_str(exc)}. Return dicts, lists, text, numbers.",
        )
    if size > limit_kb * 1024:
        return _error(
            "OutputTooLarge",
            f"the result is {size / 1024:.1f} KB as JSON but limits.output_kb is {limit_kb}. Return a smaller summary.",
        )
    return None


def _run_tool(request: dict, workspace: str, tf_dir: str, out: _Tail, err: _Tail, started: float) -> dict:
    import toolforge_sdk  # found next to this file, before the package's folders are on the path

    toolforge_sdk._configure(workspace)
    package = os.path.join(workspace, "pkg")
    limits = request.get("limits") or {}
    limit_kb = limits.get("output_kb")
    limit_kb = (
        limit_kb
        if isinstance(limit_kb, int) and not isinstance(limit_kb, bool) and limit_kb > 0
        else DEFAULT_OUTPUT_KB
    )
    _apply_limits(limits)
    sys.path[:0] = [
        package,
        os.path.join(package, ".deps"),
    ]  # the package wins over anything else, except the SDK loaded above
    os.chdir(workspace)  # so a relative `inputs/data.csv` is /workspace/inputs/data.csv
    entrypoint = request["entrypoint"]
    sys.argv[:] = [os.path.join(package, entrypoint.rpartition(":")[0])]
    sys.stdout, sys.stderr = out, err
    result: object = None
    error: dict | None = None
    try:
        function = _load_entry(package, entrypoint)
        result = function(request["args"])
    except toolforge_sdk.ToolError as exc:
        error = _error("ToolError", _safe_str(exc))
    except BaseException as exc:  # includes SystemExit and KeyboardInterrupt: whatever the tool does, the envelope still goes out
        error = _crash(exc)
    if error is None:
        error = _check_result(result, limit_kb)
    return {
        "ok": error is None,
        "result": result if error is None else None,
        "error": error,
        "stdout_tail": out.tail(),
        "stderr_tail": err.tail(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "truncated": out.total > TAIL_CHARS or err.total > TAIL_CHARS,
    }


def _emit(channel: int, nonce: str, envelope: dict) -> None:
    """Write the envelope on the private copy of fd 1, with `<` and `>` escaped so the markers can never occur inside it."""
    try:
        body = json.dumps(envelope, ensure_ascii=True, allow_nan=False)
    except BaseException as exc:  # the tool changed its result after it was checked
        fallback = _error("NotJson", f"the result could not be written as JSON ({type(exc).__name__})")
        body = json.dumps({**envelope, "ok": False, "result": None, "error": fallback}, ensure_ascii=True)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e")
    data = f"<<TF:{nonce}>>{body}<<TF-END:{nonce}>>\n".encode("ascii")
    for stream in (
        sys.__stdout__,
        sys.__stderr__,
    ):  # anything the tool left buffered must not land after the envelope
        try:
            stream.flush()
        except BaseException:
            pass
    view = memoryview(data)
    while view:
        view = view[os.write(channel, view) :]


def main(argv: list[str]) -> None:
    _reexec_with_fixed_hash_seed()
    started = time.monotonic()
    sys.dont_write_bytecode = True
    nonce = argv[1] if len(argv) > 1 else ""
    tf_dir = os.path.dirname(os.path.abspath(__file__))
    workspace = os.path.dirname(tf_dir)
    try:
        channel = os.dup(1)  # the tool may rebind or close sys.stdout; this copy of the real one stays ours
    except OSError:
        channel = 1
    out, err = _Tail(), _Tail()
    try:
        with open(os.path.join(tf_dir, "in.json"), encoding="utf-8") as handle:
            request = json.load(handle)
        envelope = _run_tool(request, workspace, tf_dir, out, err, started)
    except (
        BaseException
    ) as exc:  # the request itself was unreadable: a bug in the host or a damaged workspace
        envelope = {
            "ok": False,
            "result": None,
            "error": _crash(exc),
            "stdout_tail": out.tail(),
            "stderr_tail": err.tail(),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "truncated": False,
        }
    _emit(channel, nonce, envelope)
    os._exit(
        0
    )  # not a normal exit: no buffered output, atexit hook or thread of the tool may write after the envelope


if __name__ == "__main__":
    main(sys.argv)
'''
