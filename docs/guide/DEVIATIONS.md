# Deviations

Where reality differed from the guide, newest first: what the guide said, what is true, what was done.

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
