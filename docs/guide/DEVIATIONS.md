# Deviations

Where reality differed from the guide, newest first: what the guide said, what is true, what was done.

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
