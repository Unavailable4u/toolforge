# Progress

Source of truth for which patch comes next. An agent finishing a patch ticks its box and fills in "Notes for the next
patch" in the same patch. Never tick a box for work that is not in the patch. Box states: `[ ]` not done, `[x]` done,
`[-]` dropped on purpose (add the reason in the Notes and update every document that mentions it; see
`docs/guide/05-milestones.md`).

| Done | Patch | Title | Depends on |
| --- | --- | --- | --- |
| [x] | P01 | Skeleton, tooling, CI | none |
| [x] | P02 | Tool package spec and validation | P01 |
| [x] | P03 | Registry: lifecycle, blobs, search | P02 |
| [x] | P04 | Sandbox seam and MiniLocker runner | P01 |
| [x] | P05 | Execution contract and `run` path | P02, P04 |
| [ ] | P06 | Verification pipeline: gates G1 to G4 | P03, P05 |
| [ ] | P07 | MCP server v1 | P03, P05, P06 |
| [ ] | P08 | Containment gate G6 and Blast Radius per tool | P06 |
| [ ] | P09 | Permissions, approvals, egress and brokered secrets | P07, P08 |
| [ ] | P10 | Independent challenger, gate G5 | P06 |
| [ ] | P11 | Server-side builder | P07, P10 |
| [ ] | P12 | Reuse ranking, self-repair, rollback | P07, P11 |
| [ ] | P13 | Stateful sessions | P05, P08 |
| [ ] | P14 | Runtime image and adapter kits (software use) | P13 |
| [ ] | P15 | Forge Attack Lab | P09, P10 |
| [ ] | P16 | Benchmark harness | P11, P12, P14 |
| [ ] | P17 | Dashboard | P09, P12, P13 |
| [ ] | P18 | Demo, packaging, project docs | P15, P16, P17 |
| [ ] | P19 | Landing site | P18 |
| [ ] | P20 | Guided tour | P19 |
| [ ] | P21 | Hardening pass and release | P01 to P20 |
| [ ] | S01 | Optional: portable bundles and signed provenance | P12 |

Milestones (M0 to M7) and what can be dropped: `docs/guide/05-milestones.md`.

## Notes for the next patch

Done: P01 (skeleton), P02 (package spec), P03 (registry), P04 (sandbox seam), P05 (execution contract and `run`). Next: P06 (needs P03 and
P05, both done). P07 needs P03, P05 and P06. Decisions and interfaces that later patches must respect:

* Language is Python 3.12. Do not propose another. Runtime pins: pydantic 2.14.3, pydantic-settings 2.16.3, jsonschema 4.26.0; dev:
  pytest 9.2.0, ruff 0.17.5 (line length 110, rules E4 E7 E9 F I). Build backend setuptools 84.2.1, src layout.
* `toolforge.config.Settings` is the only reader of the environment (`TOOLFORGE_` prefix, `.env` read from the current directory).
  `load_settings()` turns bad values into `ConfigError`. Secret fields are `SecretStr`; `Settings.masked()` is what `toolforge config`
  prints. `build_allow` is a list (comma-separated in the environment). The challenger falls back to the builder through the
  `effective_challenger_*` properties. A new setting needs an `.env.example` line (a drift test enforces it).
* `toolforge.cli`: add a subcommand by writing `register(subparsers)` in your module and appending it to `cli.REGISTRARS`; set
  `handler` with `set_defaults`. `ToolForgeError` raised by a handler prints `error [code]: message` and `hint:` and exits 1.
* `tests/conftest.py`: `docker`, `llm` and `attacks` tests skip themselves; an autouse fixture removes `TOOLFORGE_*` variables and
  changes directory to `tests/` so no stray `.env` is read; `tmp_home` gives a throwaway data directory. P02 added `settings`,
  `make_payload` (a fresh valid submission dict) and `inline_schema_check` (skips the schema-check subprocess for speed). P03 added
  `registry` (a `Registry` in a temp dir), `make_package(name, version, network=, secrets=, ...)` and `activate(record)` (walks a version
  to `active`).
* P02 interfaces. `toolforge.models.package`: `ToolPackage` (`from_dir`, `from_submission`, `validate(settings)`, `manifest`, `to_tar()`,
  `code_sha`, `to_dir`), `Manifest`, `ISSUE_CODES` (the one catalogue of issue codes, severity, rule and hint; `package-format.md` embeds
  it and a drift test checks it) and `name_problem`. `toolforge.models.results`: `Issue(path, code, message, hint)` and
  `ValidationReport(issues, warnings, ok)`. `toolforge.packaging`: directory reading and writing and the deterministic `build_tar`.
  `toolforge.schemacheck.check_instances(checks, ...)`: validate values against an author's schema in a child process with a timeout.
  Anything on the host that validates caller data against an author's schema (P05 argument check, P06 fuzz inputs) must use it, never
  `jsonschema` directly, so a regex or reference bomb cannot hang the server.
* `ToolPackage.files` does not include `manifest.json`; `to_tar()` writes the manifest in canonical form (sorted keys), so `code_sha` does
  not depend on key order or on omitted defaults. `to_tar`, `code_sha` and `manifest` raise `ValidationError` on an invalid package, so
  call `validate` first. `code_sha` of the same content is pinned by a golden-value test: changing the archive format changes every hash.
* `validate` reports at most 100 errors and 100 warnings, and when a package has more than 40 files it reports only `files.too_many`
  (it does not look inside). Warnings are produced only once the manifest is valid.
* P05 settled the reference-pack question: the harness's working directory is `/workspace`, so `inputs/data.csv` resolves as given and
  `toolforge_sdk.input_path` accepts the same string; both packs are unchanged. `tests/test_run_path_local.py` runs both through
  `ToolRunner` and the real harness (stand-in sandbox, our own code only) and they return exactly their manifest examples.
* P06: `toolforge new` scaffolds `tests/test_tool.py` with three tests that fail against a `None`-returning stub (checked in
  `tests/test_cli_package.py`); the reference packs have five tests each. Both example packs return exactly their manifest example outputs (checked by
  `tests/test_run_path_local.py`); P06 still has to check them inside a sandbox with the real gates.
* P03 interfaces. `from toolforge.registry import Registry`; `Registry(home)` or `Registry.from_settings(settings)`, a context manager.
  Versions: `add_version(package, created_by=)` (status `submitted`; the package must already be valid; `version_exists` on a repeat),
  `get_version`, `find_version`, `list_versions(name=, status=)`, `active_version`, `list_tools`, `get_tool`. State changes only through
  `transition(version_id, to, actor=, reason=)`, which returns the ledger event (the registry never writes ledgers; `event["related"]`
  holds the `forge.deprecated` events of versions displaced by a promotion, which belong in their own ledgers). Also `search(query,
  limit)`, `record_call(...)`, `stats(id | "name@version" | "name")`, `record_gate(...)` / `gate_results`, and approval storage
  (`add_approval`, `decide_approval`, `list_approvals`; statuses `pending`, `approved`, `denied`). `registry.blobs` is the blob store.
  Migrations: append a `Migration` to `registry.db.MIGRATIONS` (P12 and P13 do); never edit one. `lifecycle.EDGES` is the one state
  machine (events, operator-only, reason, network/secrets rules); `docs/registry.md` embeds a table made from it.
* P03 rules the callers must follow. Pass `actor="operator"` for promote, approve, release and rollback, and a reason for the last three
  and for promote; no other actor is accepted, so the pipeline and any server code must use another name (`pipeline`, `builder`,
  `timeout`). `verified` and `verified_basic` versions that declare network or secrets can only go `pending_approval -> active`.
* P06: G2 stores the blob with `registry.blobs.put(...)` then `registry.attach_blob(version_id, sha)` (allowed once, only while the
  version is `submitted` or `verifying`). With no `requirements` the blob is `package.to_tar()`, so `blob_sha == code_sha`. Gate detail
  over 64 KB is replaced by a size marker; keep details small and sanitised.
* P07/P09: keep `Registry.transition`, `decide_approval` and `attach_blob` out of the MCP server; P09 adds the `ast` test. The registry
  cannot authenticate the "operator" string. A registry never records who made past moves: the ledger does, so append every event.
* P12: search ranking is `search.search`/`BM25_WEIGHTS`; `Hit` has name, version, description, score, status, uses, success_rate. Without
  FTS5 the fallback scores between 0 and 1 and is not comparable with bm25. Success rate is over the last 50 calls, `None` under 5.
* Not run in P03: nothing needing Docker or an LLM exists yet. The registry tests need FTS5 (present in this SQLite build, checked); the
  fallback is tested by simulation.
* CI actions are pinned by commit SHA (checkout v7.0.1, setup-python v7.1.0, looked up on 2026-11-19).
* A tool reaches `active` before P09/P10 exist only through `toolforge promote` (P07). There is no other path.
* `verified_basic` with network or secrets goes to `pending_approval` (P09), never through `promote`. `release` and manual
  `quarantine` are operator CLI commands from P08; `rollback` (P12) may only target a version that was active before.
* A problem report never quarantines a tool; only measured failures and `PolicyViolation` do (P12).
* Every patch that adds an MCP tool or a setting updates `META_TOOLS`, the README table, `docs/clients.md` and `.env.example` in the
  same patch. `scripts/check_guide.py` must pass whenever the guide or `PROGRESS.md` changes.
* `META_TOOLS` (P07) and `GATE_INFO` (P06) are the single sources for the tool list and the gate list.
* The website repository URL needs no manual edit: P19 resolves it from `--repo-url`, `site.json`, `GITHUB_REPOSITORY` or the
  git `origin` remote. Create the GitHub repository and add the remote before the first `--release` build.
* P04 interfaces. `toolforge.sandbox`: `SandboxSpec` (validates itself), `RunOutput`, `AuditSummary`, the `SandboxRunner` / `SandboxHandle`
  protocols, `FakeRunner(script, available=, hardening=, measured=)` (records `.opened` and `.handles`, never runs code) and, loaded lazily,
  `MiniLockerRunner(settings=, pool=)` and `EgressPool`. `open` raises `SandboxUnavailable` (Docker, image, submodule or decoys missing) and
  `put` / `run` / `freeze` / `thaw` raise `SandboxError`; call `runner.shutdown()` once when a process is done with sandboxes. `put` only
  writes under `/workspace` and `/tmp`; a relative path resolves under `/workspace`. `toolforge.ledger.ToolforgeLedger(path, task_id)`
  (`append`, `events`, `verify`), `toolforge.textsafety` (`sanitize`, `scan`, `fence`, `using_minilocker`).
* P04 tests. The `minilocker` fixture in `tests/conftest.py` skips a test unless the real MiniLocker imports. The import boundary lives in
  `tests/test_import_boundary.py` (`ALLOWED_IMPORTERS`; `LAZY_ONLY_ROOTS` covers `minilocker`, `docker`, `requests`, imported only inside
  functions): P08 and P14 extend it in their own patch with a dated line in `DEVIATIONS.md`. Do not name a unit test `test_minilocker_runner.py`
  (it would clash with the one in `tests/docker/`, there is no `__init__.py`).
* P04 doctor. Checks are `Check(name, required, fn)` entries in `cli_doctor.CHECKS`; add yours to that list (P10: the "challenger model equals
  builder model" warning; P13: make `--clean` also remove session containers). A check that raises is shown as FAIL, an optional check that
  fails is shown as a warning, and doctor never starts a container or pulls an image.
* P04 known limits (also in `03-minilocker-integration.md`): one global egress allowlist that only grows; two ToolForge processes on one
  Docker daemon share the proxy container name; egress is read after `run` returns, and an unreadable proxy logs `forge.egress.unobserved`.
* P04 on a real daemon (Docker 29.8.1, run by the person): `toolforge doctor` all ok and 15 of the 16 `docker` tests passed; the 16th failed
  because of a wrong assertion in the test itself (decoy ids such as `aws_secret` contain "secret"), fixed in `fix-p04-decoy-test.patch`.
* P04 CI: the checkout step now fetches submodules and installs `.[dev,sandbox]`, so the MiniLocker tests run rather than skip.
* P05: the runner passes `extra_env` to the box as given; keep real secrets out of `SandboxSpec.env` (brokered placeholders only, P09).
* P05 interfaces. `toolforge.services.runner`: `ToolRunner(runner, registry, settings, ledger_factory, clock=, new_id=)` with
  `run(name | name@version | VersionRecord, args, files=, session_id=, allow_status=("active",)) -> CallResult` and
  `run_unverified(package, args, files=)`; also `parse_envelope`, `repack_blob`, `Envelope`, `RegistryLike`, `LedgerLike`. Unknown tool or
  version raises `RegistryError`; a wrong status, a session tool, a missing or unusable blob and an invalid package raise
  `ToolNotRunnable` (codes `status_not_allowed`, `no_active_version`, `session_not_built`, `no_blob`, `blob_unusable`, `package_invalid`,
  `no_registry`). Bad arguments or files are a failed `CallResult` (`InvalidArguments`, `InvalidFiles`), never an exception, and write
  nothing. P06 must pass `allow_status=("submitted", "verifying", ...)` for versions that are not active yet, and `files={"inputs/<name>": bytes}`.
  `toolforge.models.results`: `CallResult`, `CallError` (host-only types `Timeout`, `SandboxCrashed`, `NoResult`; harness types
  `ToolError`, `Crash`, `NotJson`, `OutputTooLarge`). The sandbox spec uses `network="none"` for every call until P09, and `limits`
  come from the manifest, else `Settings.default_timeout_s` / `default_memory_mb` / 64 KB. Only a `ToolError` is not counted against a
  tool's statistics; everything else that failed in the tool or the sandbox is.
* P05 harness contract (docs/guide/package-format.md, "How a tool runs"). `toolforge.sandbox.harness` holds `HARNESS_SOURCE`, the paths
  (`HARNESS_PATH`, `SDK_PATH`, `REQUEST_PATH`, `INPUTS_PREFIX`), `start_marker`/`end_marker` and `ENVELOPE_ERROR_TYPES`. Edit
  `sandbox/assets/harness.py` and paste the result into the constant; a test checks they are equal. P06 extends the harness with
  `--tests`, `--batch` and `--stub` in both copies. The harness works out its workspace from its own location, so it can run from a
  temporary folder in tests. `tests/test_harness.py` runs it only on `tests/fixtures/trusted_tools/`.
* P05 not run: the `docker` tests in `tests/docker/test_run_path.py` and `toolforge run --package examples/toolpacks/word_count --args
  '{"text": "a b c"}' --dev-unverified` (no Docker daemon where the patch was written). Known limits: `output_path` files are discarded
  (P14); the runner records `egress_attempts` and `decoy_events` counts in the ledger but nothing acts on them until P08; a result is
  held in memory up to `limits.output_kb * 6 + 128 KB` of stdout before it is cut.
* P05 housekeeping: the guide's `core/` is `services/` in the code (see `DEVIATIONS.md`); P06 to P13 should put their modules there.
