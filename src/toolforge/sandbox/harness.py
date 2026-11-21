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

Usage: `python /workspace/.tf/harness.py <nonce> [--tests [--stub] | --batch]`. Without a flag it runs one call (below). The host
(toolforge.services.runner) has put these next to it before the call:

    <workspace>/pkg/            the unpacked tool package (and pkg/.deps when it has requirements)
    <workspace>/inputs/         files the caller provided for this call
    <workspace>/.tf/in.json     {"args": {...}, "entrypoint": "tool.py:run", "limits": {...}}
    <workspace>/.tf/toolforge_sdk.py

The result is one line on the real stdout: `<<TF:{nonce}>>{json}<<TF-END:{nonce}>>`. The tool's own prints are captured into short
tails inside the envelope and are never mixed into it. The tool runs in this process, so it can forge an envelope; that only lets
it report a different result for itself, which it could return honestly, so the host treats the envelope as untrusted data and
checks it. Nothing here is a security boundary: the container is.

`--tests` runs the package's own tests (tests/test_*.py, top-level `test_*` functions and unittest.TestCase methods, no pytest, no
fixtures) one per forked process and reports counts and the first failures as the envelope's result; with `--stub` the entrypoint
function is replaced by one that returns None first, which shows whether the tests constrain behaviour at all. `--batch` runs the
entrypoint once per item of in.json's "items", each in a forked process with its own timeout, and reports one outcome per item
(gate G3 uses it for the manifest examples, gate G4 for schema fuzzing). Items share the workspace's files, never memory.

The contract is documented in docs/guide/package-format.md ("How a tool runs"). Keep this file dependency free.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import select
import shutil
import signal
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
TEST_TIMEOUT_S = 10  # one test, unless the request says otherwise
TESTS_DEADLINE_S = 120  # all tests together, unless the request says otherwise
MAX_TESTS = 500
MAX_TEST_FILES = 50
MAX_LISTED = 20  # failures and errors listed in a test report (the counts are always complete)
MAX_BATCH_ITEMS = 500
ITEM_TEXT_CHARS = 600  # a batch item's message and trace tail
CHILD_RESULT_BYTES = 256 * 1024  # a test child reports a few lines; more than this is a bug or an attack


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


# --- Forked children: one test or one batch item per process ----------------------------------------------------------------


def _positive(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def _spawn(job, timeout_s: float, max_bytes: int) -> tuple[str, object]:
    """Run `job()` in a forked child and return what it reported.

    `("done", value)` when the child wrote a JSON value in time, `("timeout", None)` when it did not finish, `("died", text)` when it
    ended without a result (killed by a signal, out of memory, `os._exit`), `("toolarge", None)` when it wrote more than
    `max_bytes`. The child is its own process group leader and the whole group is killed afterwards, so nothing a tool started
    outlives its call. A tool that is stuck cannot stall the harness: the parent only reads, waits and kills.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: never returns
        try:
            os.setsid()
            os.close(read_fd)
            try:
                body = json.dumps(job(), ensure_ascii=True, allow_nan=False).encode("ascii")
            except BaseException as exc:  # includes SystemExit from the tool
                body = json.dumps({"__crash": _crash(exc)}).encode("ascii")
            view = memoryview(body)
            while view:
                view = view[os.write(write_fd, view) :]
        finally:
            os._exit(0)
    os.close(write_fd)
    chunks: list[bytes] = []
    total = 0
    kind = "done"
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kind = "timeout"
                break
            ready, _, _ = select.select([read_fd], [], [], min(remaining, 0.25))
            if not ready:
                continue
            data = os.read(read_fd, 65536)
            if not data:
                break
            total += len(data)
            if total > max_bytes:
                kind = "toolarge"
                break
            chunks.append(data)
    finally:
        os.close(read_fd)
        for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try:
                kill()
            except OSError:
                pass
        try:
            _, status = os.waitpid(pid, 0)
        except OSError:
            status = 0
    if kind != "done":
        return kind, None
    if not chunks:
        if os.WIFSIGNALED(status):
            return "died", f"killed by signal {os.WTERMSIG(status)}"
        return "died", "the process ended without a result"
    try:
        return "done", json.loads(b"".join(chunks))
    except ValueError:
        return "died", "the process reported something that is not JSON"


def _prepare_inputs(workspace: str, files: object) -> None:
    """Make `inputs/` hold exactly `files` ({"inputs/name": "text"}), so one item's files never leak into the next."""
    inputs = os.path.join(workspace, "inputs")
    shutil.rmtree(inputs, ignore_errors=True)
    os.makedirs(inputs, exist_ok=True)
    if files is None:
        return
    if not isinstance(files, dict):
        raise ValueError("files must be an object")
    for path, text in files.items():
        parts = path.split("/") if isinstance(path, str) else []
        if (
            len(parts) < 2
            or parts[0] != "inputs"
            or any(p in ("", ".", "..") for p in parts)
            or "\x00" in path
        ):
            raise ValueError(f"the input file name {path!r} is not 'inputs/<relative path>'")
        if not isinstance(text, str):
            raise ValueError(f"the content of {path!r} must be text")
        target = os.path.join(workspace, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)


def _item_job(request: dict, workspace: str, tf_dir: str) -> dict:
    out, err = _Tail(), _Tail()
    return _run_tool(request, workspace, tf_dir, out, err, time.monotonic())


def _item_error(kind: str, message: str, trace: str = "") -> dict:
    return {"type": kind, "message": _clip(message, ITEM_TEXT_CHARS), "trace_tail": trace[-ITEM_TEXT_CHARS:]}


def _batch_item(
    index: int, kind: str, data: object, include_results: bool, started: float, timeout_s: int
) -> dict:
    item: dict = {"index": index, "ok": False, "error": None, "result_bytes": 0, "stderr_tail": ""}
    if kind == "done" and isinstance(data, dict) and "__crash" in data:
        crash = data["__crash"] if isinstance(data["__crash"], dict) else {}
        item["error"] = _item_error(
            "Crash", str(crash.get("message", "crashed")), str(crash.get("trace_tail", ""))
        )
    elif kind == "done" and isinstance(data, dict) and data.get("ok") is True:
        item["ok"] = True
        try:
            item["result_bytes"] = len(
                json.dumps(data.get("result"), ensure_ascii=False).encode("utf-8", "replace")
            )
        except (TypeError, ValueError):
            item["result_bytes"] = 0
        if include_results:
            item["result"] = data.get("result")
    elif kind == "done" and isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        name = error.get("type") if error.get("type") in ERROR_TYPES else "Crash"
        item["error"] = _item_error(name, str(error.get("message", "")), str(error.get("trace_tail", "")))
        item["stderr_tail"] = str(data.get("stderr_tail", ""))[-ITEM_TEXT_CHARS:]
    elif kind == "timeout":
        item["error"] = _item_error("Timeout", f"no result within {timeout_s} seconds")
    elif kind == "toolarge":
        item["error"] = _item_error("OutputTooLarge", "the call wrote far more than limits.output_kb allows")
    else:
        item["error"] = _item_error(
            "Crash", str(data) if kind == "died" else "the call reported nothing usable"
        )
    item["duration_ms"] = int((time.monotonic() - started) * 1000)
    return item


def _run_batch(request: dict, workspace: str, tf_dir: str) -> dict:
    items = request.get("items")
    if not isinstance(items, list) or len(items) > MAX_BATCH_ITEMS:
        raise ValueError(f"items must be a list of at most {MAX_BATCH_ITEMS} calls")
    limits = request.get("limits") or {}
    timeout_s = _positive(limits.get("timeout_s"), 10)
    limit_kb = _positive(limits.get("output_kb"), DEFAULT_OUTPUT_KB)
    include_results = request.get("include_results") is True
    deadline = time.monotonic() + _positive(request.get("deadline_s"), 3600)
    reported: list[dict] = []
    not_run = 0
    for index, item in enumerate(items):
        if time.monotonic() >= deadline:
            not_run = len(items) - index
            break
        started = time.monotonic()
        try:
            if not isinstance(item, dict) or not isinstance(item.get("args"), dict):
                raise ValueError("each item needs an args object")
            _prepare_inputs(workspace, item.get("files"))
        except ValueError as exc:
            reported.append(
                _batch_item(
                    index,
                    "done",
                    {"ok": False, "error": _error("Crash", str(exc))},
                    False,
                    started,
                    timeout_s,
                )
            )
            continue
        call = {"args": item["args"], "entrypoint": request["entrypoint"], "limits": limits}
        kind, data = _spawn(
            lambda call=call: _item_job(call, workspace, tf_dir),
            timeout_s,
            limit_kb * 1024 * 6 + CHILD_RESULT_BYTES,
        )
        reported.append(_batch_item(index, kind, data, include_results, started, timeout_s))
    return {"items": reported, "not_run": not_run}


# --- The tests of a package ----------------------------------------------------------------------------------------------------


def _is_test_case(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name in ("TestCase", "IsolatedAsyncioTestCase"):
            return True
    return False


def _discover(package: str) -> tuple[list[dict], list[dict], int]:
    """(tests, problems, files): found by reading the files with `ast`; no test code is imported here."""
    root = os.path.join(package, "tests")
    found: list[str] = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "__")))
        found += [
            os.path.join(folder, n) for n in sorted(names) if n.startswith("test_") and n.endswith(".py")
        ]
    tests: list[dict] = []
    problems: list[dict] = []
    for path in found[:MAX_TEST_FILES]:
        rel = os.path.relpath(path, package).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=rel)
        except (SyntaxError, ValueError, UnicodeDecodeError, OSError) as exc:
            problems.append(
                {
                    "id": rel,
                    "error": _clip(f"cannot read the test file: {type(exc).__name__}: {_safe_str(exc)}", 300),
                }
            )
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                entry = {"id": f"{rel}::{node.name}", "file": rel, "kind": "function", "name": node.name}
                if isinstance(node, ast.AsyncFunctionDef):
                    entry["error"] = "async tests are not supported; call the tool function directly"
                elif node.args.args or node.args.kwonlyargs or node.args.vararg or node.args.kwarg:
                    entry["error"] = (
                        "the test takes arguments; fixtures and parametrize are not supported, use plain functions"
                    )
                tests.append(entry)
            elif isinstance(node, ast.ClassDef) and _is_test_case(node):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith(
                        "test"
                    ):
                        tests.append(
                            {
                                "id": f"{rel}::{node.name}.{member.name}",
                                "file": rel,
                                "kind": "method",
                                "cls": node.name,
                                "name": member.name,
                            }
                        )
    if len(tests) > MAX_TESTS:
        problems.append({"id": "tests/", "error": f"more than {MAX_TESTS} tests; keep the suite small"})
        tests = tests[:MAX_TESTS]
    return tests, problems, min(len(found), MAX_TEST_FILES)


def _where(exc: BaseException, package: str) -> str:
    """` (tests/test_x.py:12)` for the innermost frame inside the package, or nothing."""
    try:
        frames = [
            f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith(package + os.sep)
        ]
        if frames:
            return f" ({os.path.relpath(frames[-1].filename, package)}:{frames[-1].lineno})"
    except BaseException:
        pass
    return ""


def _describe(exc: BaseException, package: str) -> str:
    message = _safe_str(exc).strip().splitlines()
    first = message[0] if message else ""
    return _clip(f"{type(exc).__name__}: {first}{_where(exc, package)}", 300)


def _install_stub(entrypoint: str) -> None:
    """Import the entrypoint's module under its natural name and replace the function with one that returns None."""
    path, _, function = entrypoint.rpartition(":")
    name = path[: -len(".py")].replace("/", ".") if path.endswith(".py") else ""
    if not name or not function or name.split(".")[0] in sys.stdlib_module_names:
        raise ValueError(f"cannot stub the entrypoint {entrypoint!r}")
    module = importlib.import_module(name)
    if not callable(getattr(module, function, None)):
        raise ValueError(f"{path} has no function {function}")
    setattr(module, function, lambda *args, **kwargs: None)


def _test_job(package: str, workspace: str, test: dict, request: dict, stub: bool) -> dict:
    import unittest  # before the package's folders are on the path, so a package cannot replace it

    import toolforge_sdk

    toolforge_sdk._configure(workspace)
    _apply_limits({"timeout_s": _positive(request.get("test_timeout_s"), TEST_TIMEOUT_S)})
    folder = os.path.dirname(os.path.join(package, test["file"]))
    sys.path[:0] = [package, os.path.join(package, ".deps"), folder]
    os.chdir(workspace)
    sys.argv[:] = [os.path.join(package, test["file"])]
    sys.stdout, sys.stderr = _Tail(), _Tail()
    try:
        if stub:
            _install_stub(request["entrypoint"])
        name = os.path.splitext(os.path.basename(test["file"]))[0]
        if name in sys.modules or name.split(".")[0] in sys.stdlib_module_names:
            name = "__tf_test_" + name
        spec = importlib.util.spec_from_file_location(name, os.path.join(package, test["file"]))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {test['file']}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        if test["kind"] == "function":
            getattr(module, test["name"])()
            return {"outcome": "passed"}
        case = getattr(module, test["cls"])(test["name"])
        result = unittest.TestResult()
        case.run(result)
        if result.errors:
            return {"outcome": "error", "error": _clip(_first_line(result.errors[0][1]), 300)}
        if result.failures:
            return {"outcome": "failed", "error": _clip(_first_line(result.failures[0][1]), 300)}
        if result.skipped:
            return {"outcome": "skipped"}
        return {"outcome": "passed"}
    except unittest.SkipTest:
        return {"outcome": "skipped"}
    except AssertionError as exc:
        return {"outcome": "failed", "error": _describe(exc, package)}
    except BaseException as exc:
        return {"outcome": "error", "error": _describe(exc, package)}


def _first_line(trace: str) -> str:
    """The last line of a unittest traceback is `ExceptionName: message`."""
    lines = [line for line in trace.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "failed"


def _run_tests(request: dict, workspace: str, stub: bool) -> dict:
    package = os.path.join(workspace, "pkg")
    per_test = _positive(request.get("test_timeout_s"), TEST_TIMEOUT_S)
    deadline = time.monotonic() + _positive(request.get("deadline_s"), TESTS_DEADLINE_S)
    tests, problems, files = _discover(package)
    failed: list[dict] = []
    errors: list[dict] = list(problems)
    passed = skipped = ran = not_run = 0
    for index, test in enumerate(tests):
        if time.monotonic() >= deadline:
            not_run = len(tests) - index
            break
        if "error" in test:
            errors.append({"id": test["id"], "error": test["error"]})
            continue
        ran += 1
        kind, data = _spawn(
            lambda test=test: _test_job(package, workspace, test, request, stub),
            max(1.0, min(per_test, deadline - time.monotonic())),
            CHILD_RESULT_BYTES,
        )
        outcome = data.get("outcome") if kind == "done" and isinstance(data, dict) else None
        if outcome == "passed":
            passed += 1
        elif outcome == "skipped":
            skipped += 1
        elif outcome == "failed":
            failed.append({"id": test["id"], "error": _clip(str(data.get("error", "failed")), 300)})
        elif kind == "timeout":
            failed.append({"id": test["id"], "error": f"timed out after {per_test} seconds"})
        elif outcome == "error":
            errors.append({"id": test["id"], "error": _clip(str(data.get("error", "error")), 300)})
        else:
            crash = data.get("__crash") if kind == "done" and isinstance(data, dict) else None
            note = crash.get("message") if isinstance(crash, dict) else (data if kind == "died" else kind)
            errors.append({"id": test["id"], "error": _clip(f"the test process failed: {note}", 300)})
    return {
        "stub": stub,
        "files": files,
        "ran": ran,
        "passed": passed,
        "skipped": skipped,
        "failed_count": len(failed),
        "error_count": len(errors),
        "failed": failed[:MAX_LISTED],
        "errors": errors[:MAX_LISTED],
        "not_run": not_run,
    }


def _wrapped(result: dict, started: float) -> dict:
    return {
        "ok": True,
        "result": result,
        "error": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "duration_ms": int((time.monotonic() - started) * 1000),
        "truncated": False,
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
        flags = argv[2:]
        unknown = [flag for flag in flags if flag not in ("--tests", "--stub", "--batch")]
        if (
            unknown
            or ("--stub" in flags and "--tests" not in flags)
            or ("--tests" in flags and "--batch" in flags)
        ):
            raise ValueError(f"unusable options {flags!r}: use nothing, --tests [--stub] or --batch")
        if "--batch" in flags:
            envelope = _wrapped(_run_batch(request, workspace, tf_dir), started)
        elif "--tests" in flags:
            envelope = _wrapped(_run_tests(request, workspace, "--stub" in flags), started)
        else:
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
