# Conventions

## Language and tooling

* Python 3.12. Package `toolforge` in a `src/` layout. Dependencies pinned to exact versions in `pyproject.toml`.
* Pydantic v2 for every data model that crosses a boundary (package, manifest, MCP payloads, ledger events).
* `ruff` for lint and format (line length 110). `pytest` for tests. Type hints everywhere; `mypy` is not required.
* Standard library first: `sqlite3`, `tarfile`, `hashlib`, `json`, `argparse`/`typer` only if the patch says so. Add a
  dependency only when the patch lists it.

## Platforms and line endings

* All text files use LF (`.gitattributes`, `.editorconfig`). CRLF breaks `git am`, the deterministic package tar hashes and
  shell scripts. A hygiene test (P01) fails on CRLF in tracked text files.
* On Windows, develop inside WSL2 with Docker Desktop. Code uses `pathlib`, never hard-coded separators, and every path that goes
  into a sandbox is a POSIX path.

## Test markers

* no marker: unit tests. No Docker, no network, no LLM, no wall-clock sleeps. They use `FakeRunner` and fake LLMs.
* `docker`: needs a Docker daemon and the runtime image. Skipped automatically when Docker is absent.
* `llm`: needs a configured OpenAI-compatible endpoint. Skipped when `TOOLFORGE_LLM_*` is unset.
* `attacks`: the hostile-author suite against real sandboxes (needs Docker).
* `browser`: drives the built website in a headless browser (P19, P20). Skipped when Playwright is not installed.

`tests/conftest.py` (P01) registers the first four markers and the automatic skips; P19 registers `browser`.

## Configuration

All settings come from environment variables prefixed `TOOLFORGE_`, optionally loaded from a `.env` file. One
`Settings` object (pydantic-settings or a small hand-written equivalent) is the only reader of the environment.
Every setting appears in `.env.example` with a comment. A test fails if a setting is read but not documented (the
"README must not drift from code" habit, introduced in P01 and extended in later patches).

Data directory: `TOOLFORGE_HOME`, default `~/.toolforge`.

```
$TOOLFORGE_HOME/
  registry.sqlite3        metadata, lifecycle, stats
  blobs/<sha256>.tar.gz   frozen tool packages (code + vendored deps)
  ledgers/<tool>@<version>/<run-id>.jsonl   hash-chained event files
  approvals/              pending approval requests (JSON)
  sessions/               session metadata
  keys/                   signing keys (only if the optional S01 patch is built; mode 0600)
```

## Errors and messages

* Errors that an agent might read are written for an agent: say what failed, why, and what to change. Never return a
  bare stack trace through MCP. Example: `G3 failed: tests/test_tool.py::test_empty_input raised KeyError('rows').
  Handle an empty list; the manifest examples include {"rows": []}.`
* Gate failures return structured `hints`, a list of short imperative sentences.
* Host-side exceptions are logged with a correlation id; the MCP caller sees the id and a one-line cause.

## Security habits (apply in every patch)

1. Anything coming from a tool author or a tool run is **untrusted data**: code, manifest text, descriptions, outputs,
   test results, even the failure messages produced inside the sandbox.
2. Untrusted text shown to another model goes through `sanitize` and `fence` (see `03-minilocker-integration.md`) and
   has a length cap.
3. The host never executes tool code. Only the sandbox does.
4. Never put real secrets in a sandbox, a test fixture, a ledger event, a log line or a prompt.
5. Fail closed: if a gate cannot run (no Docker, no LLM), the tool does not advance past that gate. A skipped gate is
   recorded as `skipped` and caps the tool's status (see `02-architecture.md`).
6. A missing measurement is "not measured", never zero.
7. Every network listener (MCP over HTTP, the dashboard) binds to loopback by default and requires a bearer token on any
   other host. Nothing that can approve, release, roll back or delete is reachable over MCP.

## One source of truth per list

The MCP tool list (`META_TOOLS`, P07), the gate list (`GATE_INFO`, P06), the settings (`Settings`), the attack catalogue and the
benchmark tasks are each defined once in code. The README, the docs and the website are generated from them or checked against
them by drift tests. Never hand-copy such a list into a document.

## Ledger events

Names are dotted and lower case. Payloads contain ids, hashes, counts and labels, never secret values. ToolForge events
use the prefix `forge.`; events produced by MiniLocker's own `Watch` keep their native names so MiniLocker's report
builder can read them. The catalogue is in `02-architecture.md` and each patch adds its events there (update that
section in the same patch).

## Docs and comments

* Docstrings explain intent and constraints, not what the next line does.
* Each module that enforces a security property says so in its docstring and names the test that proves it.
* Public behavior is documented in the README or `docs/`; a test fails when the documented MCP tool list and the real
  one differ (introduced in P07).

## The website (P19, P20)

Static files, no external requests at build time or run time, no analytics, no number that is not in a results file ("Not
measured yet" otherwise). The audience, writing rules and design direction are in `site-brief.md`.

## Git

One patch = one commit = one `format-patch` file. Commit subject `Pxx: <title>`. No force pushes, no history rewrites.
