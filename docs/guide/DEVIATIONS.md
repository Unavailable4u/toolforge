# Deviations

Where reality differed from the guide, newest first: what the guide said, what is true, what was done.

* 2026-11-21 (P06): the guide puts the forge in `src/toolforge/core/forge.py`. It is `src/toolforge/services/forge.py`, next to the
  runner (see the P05 entry on `core/`). The gates are in `src/toolforge/verify/` as the guide says.
* 2026-11-21 (P06): the guide's pip command for G2 is `pip install --no-input --disable-pip-version-check --no-deps --only-binary=:all:
  --target /workspace/pkg/.deps -r requirements.txt`. The command also has `--no-compile` and `--no-cache-dir`, so the frozen package holds no
  `.pyc` files or cache and is the same every time. The requirements file is `/workspace/requirements.txt`, written by the host.
* 2026-11-21 (P06): G4's number of inputs is a constructor argument of the gate (`FuzzGate(fuzz_inputs=30)`, `toolforge verify
  --fuzz-inputs N`, 1 to 200), not a setting, so `.env.example` did not change. The guide says "configurable" without saying where.
* 2026-11-21 (P06): the guide lists the harness modes `--tests`, `--batch` and `--stub`; `--stub` is a flag of `--tests`. `--batch` runs
  every input in its own forked process with its own time limit and a rebuilt `inputs/` folder; items share workspace files, never memory.
  A test that cannot be discovered by reading the syntax tree (a fixture, `parametrize`, async) is reported as an error, not skipped.
* 2026-11-21 (P06): the guide says gates that are not built are skipped. G5 and G6 are recorded as `skipped` rows in every run (so the
  report lists six gates and a version ends `verified_basic`); G7 is not part of the pipeline because it is the operator's approval
  (P09), not a step the pipeline runs. `PIPELINE_GATE_IDS` is G1 to G6.
* 2026-11-21 (P06): the guide does not say what happens when the sandbox itself is unavailable. `SandboxUnavailable` and `SandboxError`
  are raised to the caller, `forge.verification.aborted` is written, the version stays `verifying`, and submitting the same content again
  resumes it (all gates run again from G1). The package is not blamed for an infrastructure failure.
* 2026-11-21 (P06): the guide says an invalid package is rejected. It is rejected with the pipeline's report but is not registered,
  because the registry holds only valid manifests; so nothing is stored about it and it can be fixed and submitted again under the
  same version. A package that is valid but fails a gate is registered as `rejected` and its version number is used up.
* 2026-11-21 (P06): G4 found a real fault in the reference pack `word_count`: a single very long word made `top_words` return an
  unbounded string. The pack now shows at most `MAX_SHOWN_CHARS = 64` characters of a word (counts are still exact) and has a new test.
  Its manifest example is unchanged. The pack's behaviour for ordinary text is the same.
* 2026-11-21 (P06): `GateResult.detail["warnings"]` carries a gate's warnings (G1's advisory findings, G4's "every input was
  refused"); the pipeline collects them into `VerificationReport.warnings`. Warnings never change a gate's status.
* 2026-11-21 (P06): scope additions the guide does not list: `verify/sandboxrun.py` (what G2 to G4 share: opening a sandbox, running
  the harness, reading reports defensively), `src/toolforge/cli_verify.py` (registered through `cli.REGISTRARS`), `services.runner.sdk_source()`
  (public), `docs/verification.md`, and the test helpers `tests/local_sandbox.py`, `tests/verify_helpers.py`, `tests/sandbox_scripts.py`.
  `tests/test_harness.py`'s import allow-list for the harness gained `ast`, `select`, `shutil`, `signal` and `unittest`.
* 2026-11-21 (P06): the guide says G3's pass criteria use the manifest examples "when `output` is present". Examples marked `live` are
  skipped even when they have an output, because a live example needs the network.
* 2026-11-21 (P06): P06 delivers the whole patch in one file although the guide calls it a likely candidate for splitting
  (`AGENTS.md`: propose a split before writing code). It was not split; the patch is large (`git apply --stat` shows how large).
* 2026-11-21 (P06): not run where the patch was written (no Docker daemon): the `docker`-marked tests in `tests/docker/test_verification.py`,
  the real G2 pip step with network, and `toolforge submit examples/toolpacks/csv_summary` through Docker. The gates were run end to end
  through the real harness with a stand-in sandbox (`tests/test_verify_local.py`), which runs trusted code only and has no isolation.
* 2026-11-20 (P05): the guide puts the runner in `src/toolforge/core/runner.py` and lists `core/` (forge, approvals, sessions, repair,
  builder) in the architecture. P05 puts it in `src/toolforge/services/runner.py` (`services/__init__.py` says why: these modules sit
  between the MCP server and the registry) and `02-architecture.md` now lists `services/`. The patch files P06 to P13 still say
  `core/`; read that as `services/`.
* 2026-11-20 (P05): the guide says the harness sets `PYTHONHASHSEED=0` and adds `/workspace/.tf` to `sys.path`. The hash seed cannot be
  changed once Python runs, so the harness re-executes itself once with the variable set (and `-B`); `.tf` is already on the path
  because it is the script's own folder. The harness loads `toolforge_sdk`, `json`, `traceback` and the encodings before the package's
  folders go on the path, so a package that ships its own `json.py` or `toolforge_sdk.py` cannot replace them.
* 2026-11-20 (P05): the guide says the harness is "a string constant plus a copy under `assets/`". `sandbox/harness.py` holds the
  constant (`HARNESS_SOURCE`, a raw triple single quoted string) and the host-side constants (paths, markers, error types);
  `sandbox/assets/harness.py` is the copy, and `tests/test_harness.py::test_the_asset_copy_equals_the_constant` fails when they differ.
* 2026-11-20 (P05): the guide allows running `harness.py` on the host only for fixtures in `tests/fixtures/trusted_tools/`.
  `tests/test_run_path_local.py` also runs the two reference packs in `examples/toolpacks` through `ToolRunner` and the real harness
  with a stand-in sandbox, because that is code of ours and it is the only automated check, before P06, that both packs return exactly
  their manifest examples (they do). It never runs a generated or submitted package.
* 2026-11-20 (P05): the open question about `csv_summary` and relative paths is settled: the harness's working directory is
  `/workspace`, so `inputs/data.csv` resolves as given and `input_path` accepts the same string. The pack is unchanged.
* 2026-11-20 (P05): scope additions the guide does not list: `ToolNotRunnable` in `errors.py`, `CallError` next to `CallResult` in
  `models/results.py`, `src/toolforge/cli_run.py` (registered through `cli.REGISTRARS`, like the other commands),
  `tests/test_sdk.py`, `tests/test_cli_run.py` and `tests/test_run_path_local.py`. The Docker tests in `tests/docker/test_run_path.py`
  and the `toolforge run --dev-unverified` acceptance command could not be run where the patch was written (no Docker daemon).
* 2026-11-20 (P04): the guide's CI runs `pip install -e ".[dev]"` and checks out without submodules, so every test that needs MiniLocker
  (the `minilocker` fixture) would skip silently. P04 sets `submodules: true` on the checkout step and installs `.[dev,sandbox]`
  (`sandbox` is a new optional extra: `docker==7.2.0`). The `docker`-marked tests still need a daemon and are not run in CI.
* 2026-11-20 (P04): the guide wraps MiniLocker's `Ledger` as `ToolforgeLedger(path, task_id)`; the real class is `Ledger(task_id, path=None)`
  and does not create the parent directory, does not fsync and raises a bare `json` error on a damaged file. `ToolforgeLedger` adds the
  `mkdir`, an `fsync` after each append under a lock, and `LedgerError`. `events` returns a copy, and `SandboxHandle.events()` returns a deep
  copy, so a caller cannot alter the chain in memory.
* 2026-11-20 (P04): the guide says `textsafety.fence` wraps MiniLocker's. Its `fence` header reads "UNTRUSTED WEB CONTENT", untrue for tool output,
  so `fence` is local in every case (same `<<<BEGIN UNTRUSTED nonce>>>` shape, random 16-hex nonce, body and label defanged). `sanitize` and
  `scan` use MiniLocker's `policy.injection` when importable; the fallback `scan` returns `measured: false` and no score.
* 2026-11-20 (P04): the guide's `Watch.after_tool(box, tool, result, log)` is `after_tool(box, tool, result, log, probe=False)` in the source. The
  runner passes `probe=True` and the complete output text (not a short rendering), so a decoy beyond the first few hundred characters is
  still noticed. `Sandbox` also needs `supports_measure`; a box without it makes `Watch` inert, so the runner refuses `decoys=True` for
  such a box instead of running without decoys.
* 2026-11-20 (P04): the guide says the submodule is added with the three commands under "Adding the submodule". They were run as written
  (URL `https://github.com/shaikh-dotcom/MiniLocker.git`, pin `0e76295a986292cdb07795d6efc1570f9d0c1acb`); the patch carries the gitlink and
  `.gitmodules`. A person who applies it with `git am` then runs `git submodule update --init third_party/minilocker`.
* 2026-11-20 (P04): scope additions the guide does not list: `SandboxError` and `LedgerError` in `errors.py`, `src/toolforge/cli_doctor.py` (the
  checks live in their own module, registered through `cli.REGISTRARS` like the other commands), the `sandbox` extra in `pyproject.toml`, the
  `minilocker` fixture in `tests/conftest.py`, and `tests/test_runner_with_stand_in.py` (the runner's unit tests with a stand-in box, since
  `tests/docker/` needs a daemon). The `docker`-marked tests could not be run where the patch was written.

* 2026-11-19 (P03): the guide's scope lists `registry/{db,store,lifecycle,search}.py` and the CLI. P03 also adds
  `registry/__init__.py` (the `Registry` class that wires the four together and holds the version, gate-result and approval storage,
  so callers have one object to open) and `src/toolforge/cli_registry.py` (the two commands, registered through `cli.REGISTRARS` like
  `cli_package.py`). It also adds `docs/registry.md`, with a drift-tested lifecycle table. Nothing else was added to the scope.
* 2026-11-19 (P03): the guide says `transition` "returns an event dict". Promoting a version also deprecates the previous one, and that
  event belongs in the other version's ledger (`ledgers/<tool>@<version>/`), so the returned dict carries a `related` list of such
  events (empty when there are none).
* 2026-11-19 (P03): the event catalogue had no event for `submitted -> verifying` or for a rejection by a gate. P03 adds
  `forge.verifying` and `forge.rejected` to the catalogue in `02-architecture.md` (a test checks that every event the registry returns
  is listed there). A denied approval is `forge.approval.denied`, not `forge.rejected`.
* 2026-11-19 (P03): the guide says `verified_basic -> active` is operator-only and refused for network or secrets, and the lifecycle
  diagram says `verified -> active` is for tools with "no sensitive permission". P03 enforces both in `transition` (fail closed). It
  also makes `pending_approval -> active` operator-only, since the guide says the operator approves, so no automatic actor can approve.
* 2026-11-19 (P02): the guide's scope lists `models/package.py`, `models/results.py` and `packaging.py`. Validating examples against
  a hostile schema on the host needs a time and memory limit, so P02 also adds `src/toolforge/schemacheck.py` (a child process with a
  timeout) and `src/toolforge/cli_package.py` (the two CLI commands, registered through `cli.REGISTRARS`). Nothing else was added to
  the scope.
* 2026-11-19 (P02): the guide says `ValidationReport` has `ok`, `issues` and `warnings` but names no warning. P02 defines three advisory
  warnings (`when_to_use.empty`, `output_schema.missing`, `examples.no_output`). They are produced only once the manifest itself is
  valid. An unlisted extra rule: a package may not list the same requirement name twice.
* 2026-11-19 (P02): the guide's `csv_summary` example passes `inputs/data.csv` as `path` and its manifest example gives the partial
  output `{"rows": 2}`. The reference pack in `examples/toolpacks/csv_summary` returns the full summary for its example and opens
  `args["path"]` as given, because the `toolforge_sdk` helpers (`input_path`) are specified in P05 but do not exist yet. P05 must decide
  where relative paths resolve inside the sandbox and, if needed, adjust this pack (see "Notes for the next patch" in `PROGRESS.md`).
