# Deviations

Where reality differed from the guide, newest first: what the guide said, what is true, what was done.

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
