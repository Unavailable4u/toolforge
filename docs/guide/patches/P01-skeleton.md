# P01: Skeleton, tooling, CI

Depends on: nothing.

## Goal

A runnable, linted, tested, empty package with configuration, error types, a CLI shell and CI, so every later patch only
adds behavior.

## Scope

* `pyproject.toml`: name `toolforge`, `requires-python = ">=3.12"`, src layout, console script `toolforge = toolforge.cli:main`.
  Runtime dependencies for now: `pydantic`, `pydantic-settings`, `jsonschema`. Dev: `pytest`, `ruff`. Pin exact versions
  (look up current stable versions; do not use ranges for runtime dependencies).
* `src/toolforge/__init__.py` (`__version__ = "0.0.1"`), `errors.py`, `config.py`, `cli.py`.
* `tests/conftest.py`, `tests/test_config.py`, `tests/test_cli.py`, `tests/test_docs_drift.py`, `tests/test_repo_hygiene.py`, `tests/test_guide_consistency.py` (runs `scripts/check_guide.py`, which
already exists in the starter, and asserts exit code 0).
* `.env.example`, `LICENSE` (MIT, "Copyright (c) 2026 ToolForge authors"), `Makefile` (`lint`, `fmt`, `test`, `guide`), `.github/workflows/ci.yml`.
* Replace nothing in `README.md`, `AGENTS.md`, `NOTICE.md`, `.gitattributes`, `.editorconfig`, `scripts/check_guide.py` or the guide.
  Add `[tool.ruff] line-length = 110` to `pyproject.toml`; the starter's script is already formatted for it, and `ruff` runs over it.

## Design

`errors.py`: `ToolForgeError(Exception)` base with `code: str` and `hint: str | None`; subclasses
`ConfigError`, `ValidationError`, `SandboxUnavailable`, `RegistryError`, `IllegalTransition`, `GateFailed` (more are added by
later patches; keep each in this module).

`config.py`: `Settings` with these fields (all `TOOLFORGE_` prefixed, all documented in `.env.example`):

| Field | Default | Meaning |
| --- | --- | --- |
| `home` | `~/.toolforge` | data directory (see conventions) |
| `minilocker_path` | unset | override for `third_party/minilocker/backend` |
| `runtime_image` | `python:3.12-slim` | image for tool sandboxes (P14 changes the default) |
| `build_allow` | `pypi.org,files.pythonhosted.org` | hosts reachable during the build gate |
| `max_package_kb` | `256` | cap on a package's source size |
| `default_timeout_s` | `20` | per call timeout when the manifest has none |
| `default_memory_mb` | `256` | per call memory when the manifest has none |
| `max_concurrent_verifications` | `2` | server-side semaphore |
| `auto_promote` | `true` | promote tools with no sensitive permissions after all gates pass |
| `runtime_decoys` | `true` | plant decoy credentials in every call sandbox (P08) |
| `expose_direct` | `false` | also expose active tools as individual MCP tools (P07) |
| `llm_base_url`, `llm_model`, `llm_api_key` | unset | builder LLM (P10, P11) |
| `challenger_base_url`, `challenger_model`, `challenger_api_key` | fall back to the `llm_*` values | challenger LLM |
| `server_host`, `server_port`, `server_token` | `127.0.0.1`, `8765`, unset | MCP HTTP transport (P07); a token is required on any non-loopback host |
| `dashboard_host`, `dashboard_port`, `dashboard_token` | `127.0.0.1`, `8787`, unset | dashboard (P17) |
| `approval_timeout_s` | `900` | pending approvals become denials after this |
| `log_level` | `INFO` | log verbosity (logs go to stderr, never to stdout in stdio mode) |

Later patches may add fields, always with a documented default and a `.env.example` line. Secret fields (`*_api_key`,
`*_token`) are masked everywhere they are printed or logged.

`cli.py`: `main(argv=None) -> int` using `argparse`. Subcommands now: `version`, `config` (prints effective settings with
secrets masked), `doctor` (stub that prints "no checks yet"). Later patches register subcommands through a small
`register(subparsers)` pattern so patches do not edit one giant function.

`tests/conftest.py`: register markers `docker`, `llm`, `attacks`; auto-skip `docker` tests when no daemon answers a ping
(import `docker` lazily), `llm` tests when `TOOLFORGE_LLM_BASE_URL` is unset, and `attacks` tests when Docker is absent.
Provide a `tmp_home` fixture that sets `TOOLFORGE_HOME` to a temp dir. (P19 adds the `browser` marker.)

`tests/test_docs_drift.py`: every `Settings` field appears in `.env.example` (as `TOOLFORGE_<FIELD>`), and every
`TOOLFORGE_` name in `.env.example` is a real field.

`tests/test_repo_hygiene.py`: when the repository is a git checkout, no tracked text file contains a carriage return; no
`.patch` file and no `.env` file is tracked; `.env.example` contains no value that looks like a real secret. Skipped outside a
git checkout.

CI: one job, Python 3.12, `pip install -e .[dev]`, `ruff check`, `ruff format --check`, `pytest -m "not docker and not llm and not attacks"`.

`Makefile` targets are thin wrappers over the commands in "Acceptance"; the commands themselves always work without `make`.

## Tests

Settings defaults; env override; secret masking in `config`; CLI `version` exit code 0; markers registered (no
`PytestUnknownMarkWarning`); drift test; hygiene test.

## Acceptance

```
pip install -e ".[dev]" && ruff check . && ruff format --check . && pytest -q && toolforge version
```

## Out of scope

Everything else. No registry, no sandbox, no MCP.
