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
| [x] | P06 | Verification pipeline: gates G1 to G4 | P03, P05 |
| [x] | P07 | MCP server v1 | P03, P05, P06 |
| [x] | P08 | Containment gate G6 and Blast Radius per tool | P06 |
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

Done: P01 (skeleton), P02 (package spec), P03 (registry), P04 (sandbox seam), P05 (execution contract and `run`), P06 (gates G1 to G4), P07 (MCP server v1), P08 (gate G6 and Blast Radius).
Next: P09 (permissions, approvals, egress and brokered secrets; needs P07 and P08, done). P10 (gate G5) needs only P06. Decisions and interfaces that later patches must respect:

* P08 interfaces. `verify/g6_containment.py::ContainmentGate` (G6, registered in `GATE_CLASS_PATHS`); `sandbox.base.decoy_violations` and `decoys_planted`
  (one definition shared by G6 and the runtime tripwire); `sandboxrun.open_sandbox(..., decoys=)`; `FakeRunner(events=[(type, payload)])` scripts
  `canary.*` and `egress.*` events. `report/blast_radius.py`: `generate(settings, name, version)`, `load_stored`, `summarize`, `missing_summary`,
  `render_text`, `render_html`; stored as `ledgers/<tool>@<version>/blast-radius.json` (no database column). `ledger.read_ledger`, `verify_chain`,
  `version_ledger_dir`, `OPERATOR_LEDGER`. `operator_actions.quarantine`, `release`, `release_context`; CLI `toolforge release|quarantine|report`;
  `forge_describe` returns `blast_radius`. `ToolRunner` quarantines the active version (actor `forge`) when a call touches a decoy and fails the call with
  `PolicyViolation`. P09 must add egress to the report (G6 runs with the network closed, so egress there is `not applicable`, never `clean`).
  A skipped G6 keeps a version at `verified_basic`; with the local stand-in runner G6 is always skipped. Known limit: a caller who passes a decoy path to
  a tool that opens caller-chosen paths can force a quarantine (reversible with `release`).

* P07 interfaces. `toolforge.server.tools`: `META_TOOLS` (tuple of `MetaTool(name, summary, inputs, returns, since, description)`; append your tool
  here, add its handler to `server/app.py::_handlers` (a mismatch stops the server from starting), and refresh the tables in `README.md` and
  `docs/clients.md` with `tools_markdown()`; `tests/test_docs_clients.py` checks both), `OPERATOR_ONLY` (functions the server package may never
  import; extend it and the ast test in `tests/test_server_tools.py` for release, quarantine (P08), decide (P09), rollback (P12); that test also bans
  `transition`, `decide_approval`, `attach_blob` and `add_approval` in the server package), `ToolService` (the logic, plain blocking code returning
  dicts), `STATUS_NEXT_STEP` (what to say per status). `toolforge.server.app`: `build_server(settings, registry=, sandbox=, ledger_factory=, forge=,
  tool_runner=, runtime_probe=, watch_interval_s=) -> ForgeApp`, `build_http_app`, `serve_stdio`, `serve_http`, `blocking(tool, fn)` (the error
  wrapper every handler uses: a `ToolForgeError` becomes its message, hint, code and a `ref`; anything else "an internal error" and a `ref`; server-side
  failures are never forwarded). `toolforge.server.fences`: `run_response(CallResult, label)`, `Fencer`, `clean_text`, `clean_json`, `description_flags`
  (everything a tool author or a tool run says to a model goes through these). `toolforge.netguard`: `NetGuard`, `require_token`, `is_loopback`,
  `origin_allowed`; P17 wraps the dashboard in it with `TOOLFORGE_DASHBOARD_*` values (add `dashboard_allowed_hosts` and `..._origins` settings or reuse
  the server ones, and say which in `DEVIATIONS.md`). `toolforge.services.operator_actions.promote(settings, registry, "name@version", reason,
  ledger_factory=)`: P08 adds `release` and `quarantine` beside it, P09 `decide`, P12 `rollback`; write their ledger events the same way
  (`_write_ledger`, file `ledgers/<tool>@<version>/operator.jsonl`). The CLI commands are `cli_promote.py` and `cli_serve.py`.
* P07 behaviour. `forge_find` lists active versions only; `confidence` is query-word coverage (P12 replaces it). `forge_run` runs `active` only and
  names the status and next step otherwise. `forge_submit` waits at most 30 s for a verification slot (`Forge(queue_timeout_s=)`), then answers
  `busy`; resubmitting identical content is safe; a new version of an active name must set `parent_version` to the active version or it is
  refused as `name_collision`. Reports go to the model as summaries and hints only, flagged by `textsafety.scan`, never raw gate `detail`.
  `promote` refuses a version without a blob or whose latest G2 row is not `passed`, and any version with network or secrets (P09 changes that path).
  `Pipeline.run` and `Forge.submit` take `progress=` (`ProgressFn(gate_id, phase, status)`), used for MCP progress notifications. `expose_direct`
  adds `tf_<name>` tools (stateless tools without caller files) and a registry-polling watcher; see `DEVIATIONS.md`.
* P07 test helpers: `tests/server_helpers.py` (`make_env` builds an in-memory server over a real registry with scripted gates and a stand-in tool
  runner; `Env.add_tool` verifies and promotes a tool; `Env.call` and `Env.session` run a real `mcp.Client`), and in `tests/verify_helpers.py`
  `scripted_gates()` / `BuildingGate` (a scripted G2 that stores the blob, which `promote` requires). `tests/test_server_tools.py` also runs one
  end-to-end test with the real harness through the stand-in sandbox.
* P07 not run (no Docker daemon where it was written): every `docker`-marked test, including `tests/docker/test_server_runtime_probe.py`;
  `toolforge submit examples/toolpacks/csv_summary` through real containers followed by `toolforge promote` and a client call; and any real MCP
  client program. Client configuration snippets in `docs/clients.md` are untested against those programs.
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
* P06 interfaces. `toolforge.verify.pipeline`: `GATE_INFO` (the one list of gates; `gates_markdown()`), `GATE_CLASS_PATHS` (which gates are
  built), `PIPELINE_GATE_IDS` (G1 to G6; unbuilt gates are recorded `skipped`), `Pipeline(settings, runner, registry=None, ledger_factory=,
  gates=, fuzz_inputs=).run(package, submitted_by, version=None) -> VerificationReport` (`status`, `ok`, `gates`, `warnings`, `next_action`,
  `registered`, `dry_run`, `version_id`, `as_dict()`, `text()`), `report_from_registry`, `GateResult` and `passed/failed/skipped`. A gate is a
  class with `id`, `budget_s` and `run(ctx) -> GateResult`; register it in `GATE_CLASS_PATHS` by dotted path (P08 adds G6, P10 G5) and the
  status in `GATE_INFO` turns to `built` by itself (a drift test checks that). `toolforge.services.forge.Forge(settings, registry, runner,
  ledger_factory=).submit(payload, submitted_by)` / `.submit_package(package, by)` is what the P07 `forge_submit` tool must call and
  return (a `VerificationReport`, `as_dict()` for JSON). Do not call the pipeline from the server directly: `Forge` keeps the concurrency
  limit (`max_concurrent_verifications`), refuses a changed package under an existing version and resumes a stuck one.
* P06 behaviour P07 to P09 depend on. A rejected package that was valid stays registered as `rejected` and its version number is used
  up; an invalid one is not registered. A sandbox failure raises `SandboxUnavailable` / `SandboxError` and leaves the version `verifying`
  (submitting the same content resumes it); `forge_submit` should turn that into a plain "try again later". Every verified version has
  a blob (G2 attaches it, `blob_sha == code_sha` when there are no requirements), but **promote (P07) must still refuse a version that has
  no blob or whose G2 row is not `passed`**, and P09 must check G1 to G4 rows before moving anything to `pending_approval`.
* P06 gate details. G1 findings: `FINDING_CODES` in `verify/g1_static.py` (four reject, four warn); secret patterns: `secretscan.PATTERNS`
  (`scan(text) -> [Finding(pattern, file, line)]`, never the matched text; P10 reuses it before anything goes to a model provider).
  `schemagen.generate(schema, examples, seed=, count=)` makes deterministic inputs and validates them through `schemacheck` (never
  `jsonschema` on the host). G3 uses `--tests`, the examples through `--batch`, then `--tests --stub`. G4's input count is the
  `FuzzGate(fuzz_inputs=)` argument. `verify/sandboxrun.py` is what G2 to G4 share; anything a sandbox prints is read through
  `read_test_report` / `read_batch_report` (a forged or malformed report fails the gate, never passes it). P08 (G6) should reuse it.
* P06 harness: `sandbox/assets/harness.py` gained `--tests [--stub]` and `--batch`; `HARNESS_SOURCE` is synced (test checks it).
  `services.runner.sdk_source()` is public. The tests runner supports top-level `test_*` functions and `unittest.TestCase` methods only
  (no fixtures, parametrize, conftest, async); documented in `docs/verification.md` and `package-format.md`.
* P06 test helpers: `tests/local_sandbox.py` (`LocalRunner`: a stand-in with NO isolation that runs the real harness; trusted code only,
  never a submitted package), `tests/verify_helpers.py` (`valid_package`, `MemoryLedgers`, `ScriptedGate`), `tests/sandbox_scripts.py`.
  `tests/test_verify_local.py` runs both reference packs through G1 to G4 with the real harness; `tests/docker/test_verification.py`
  does the same through MiniLocker.
* P06 not run (no Docker daemon where it was written): `tests/docker/test_verification.py` (six tests, including the real pip build for
  G2), `toolforge submit examples/toolpacks/csv_summary` through Docker, and the real timing of G2 to G4 in containers. The G4 run of
  `csv_summary` ends with a warning ("main path was not exercised") because every fuzz input is a handled refusal; that is a fact about
  the pack, not a fault. The word_count pack was changed (see `DEVIATIONS.md`) because G4 found an unbounded output.
* P06 known environment failure that is not caused by this patch: `tests/test_runner_with_stand_in.py::test_a_factory_failure_releases_the_pool_and_is_explained`
  fails on a clean checkout where the `docker` Python package is not the real one (`docker.errors` missing); it failed before P06 too.
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
* Fix P05 (`fix-p05-ledger-events-test.patch`): `ToolforgeLedger.events` is a property, and the Docker ledger test called it. Run on a real
  daemon (Docker, Python 3.14.4): the other five tests in `tests/docker/test_run_path.py` passed, and `toolforge run --package
  examples/toolpacks/word_count --args '{"text": "a b c"}' --dev-unverified` printed `"ok": true` with the expected result in 354 ms. The
  fixed test itself has not been run again where the fix was written (no daemon).
* Fix P05 (`fix-p05-ledger-forge-events-test.patch`): the same Docker test also asserted that `probe_marker` appears nowhere in the ledger
  file. MiniLocker's own `sandbox.audit` event shares the chain and reports what the sandbox ended up with, so a file name the tool left
  in `/workspace` can appear there. The test now checks what the guarantee is about: the `forge.*` events carry no tool value. Not run
  again where the fix was written (no Docker daemon). Note for P08: the audit is where Blast Radius will read workspace changes from.
