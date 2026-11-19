# Progress

Source of truth for which patch comes next. An agent finishing a patch ticks its box and fills in "Notes for the next
patch" in the same patch. Never tick a box for work that is not in the patch. Box states: `[ ]` not done, `[x]` done,
`[-]` dropped on purpose (add the reason in the Notes and update every document that mentions it; see
`docs/guide/05-milestones.md`).

| Done | Patch | Title | Depends on |
| --- | --- | --- | --- |
| [x] | P01 | Skeleton, tooling, CI | none |
| [x] | P02 | Tool package spec and validation | P01 |
| [ ] | P03 | Registry: lifecycle, blobs, search | P02 |
| [ ] | P04 | Sandbox seam and MiniLocker runner | P01 |
| [ ] | P05 | Execution contract and `run` path | P02, P04 |
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

Done: P01 (skeleton), P02 (package spec). Next: P03 (registry) or P04 (sandbox seam); both depend only on done patches. Decisions and
interfaces that later patches must respect:

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
  `make_payload` (a fresh valid submission dict) and `inline_schema_check` (skips the schema-check subprocess for speed).
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
* P05: the reference pack `examples/toolpacks/csv_summary` opens `args["path"]` as given (its manifest example passes
  `inputs/data.csv`) and falls back to a local `ToolError` when `toolforge_sdk` is absent. Decide where relative paths resolve in the
  sandbox (the package's working directory must make `inputs/data.csv` resolve to `/workspace/inputs/data.csv`, or switch the pack to
  `input_path`) and keep both packs passing. P05's docs addition belongs in `package-format.md`, section "Not built yet".
* P06: `toolforge new` scaffolds `tests/test_tool.py` with three tests that fail against a `None`-returning stub (checked in
  `tests/test_cli_package.py`); the reference packs have five tests each. The two example packs were checked by hand to return exactly
  their manifest example outputs; no test runs package code on the host, so P06 (in a sandbox) is the first automated check of that.
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
* MiniLocker's pinned commit and signatures are unverified. P04 reads the source first and records differences in
  `docs/guide/DEVIATIONS.md` (it now exists; P02 wrote the first entries).
