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
| [x] | P09 | Permissions, approvals, egress and brokered secrets | P07, P08 |
| [x] | P10 | Independent challenger, gate G5 | P06 |
| [x] | P11 | Server-side builder | P07, P10 |
| [x] | P12 | Reuse ranking, self-repair, rollback | P07, P11 |
| [x] | P13 | Stateful sessions | P05, P08 |
| [x] | P14a | Runtime image, recipes and reference packs (software use) | P13 |
| [x] | P14b | Browser plans and the GUI kit | P14a |
| [x] | P15 | Forge Attack Lab | P09, P10 |
| [x] | P16 | Benchmark harness | P11, P12, P14a, P14b |
| [x] | P17 | Dashboard | P09, P12, P13 |
| [x] | P18 | Demo, packaging, project docs | P15, P16, P17 |
| [x] | P19 | Landing site | P18 |
| [x] | P20 | Guided tour | P19 |
| [ ] | P21 | Hardening pass and release | P01 to P20 |
| [x] | S01 | Optional: portable bundles and signed provenance | P12 |
| [x] | S02 | Optional: declarative tool chains | P09, P12 |

Milestones (M0 to M7) and what can be dropped: `docs/guide/05-milestones.md`.

## Notes for the next patch

Done: P01 (skeleton), P02 (package spec), P03 (registry), P04 (sandbox seam), P05 (execution contract and `run`), P06 (gates G1 to G4), P07 (MCP server v1), P08 (gate G6 and Blast Radius), P09 (approvals, egress, brokered secrets), P10 (independent challenger, gate G5), P11 (server-side builder), P12 (reuse ranking, self-repair, rollback), P13 (stateful sessions), P14a (runtime image, recipes, reference packs), P14b (browser plans and the GUI kit), S01 (optional: portable bundles and signed provenance), S02 (optional: declarative tool chains), P15 (Forge Attack Lab), P16 (benchmark harness), P17 (dashboard), P18 (demo, packaging, project docs), P19 (landing site), P20 (guided tour).
Next: P21 (hardening pass and release; needs P01 to P20, done). Decisions and interfaces that later patches must respect:
* P20 interfaces. `scripts/build_tour_data.py [recording] [--out] [--illustrative]` (`build`, `from_recording`, `illustrative`, `validate`, `render`; `STEP_IDS`, `STEP_GATES`, `FACTS`, `METRICS`, `PERMISSIONS`, `CHOICES`, `LABELS`) writes `site/data/tour.json` (`schema` 1: `source`, `badge`, `recorded_at`, `toolforge_version`, `caveats`, `story`, `steps[]` of `id`, `gates`, `outcome`, `note`, `events[]` of `t`, `act`, `source`, `type`, `tool`, `label`, `facts`). The page is `site/src/{partials/tour.html,tour.js,tour.css,tour_captions.json,glossary.json,tour_illustrative.json,assets/tour-stage.svg}`; `build_site.py` validates the data and writes the no-JavaScript list (`read_tour`, `tour_values`). Docs: the "Tour" section of `docs/SITE.md`. Tests: `tests/test_tour_{data,captions,static}.py`, `tests/browser/test_tour_browser.py`.
* P20 rules. The recording is untrusted: no open text reaches the page, and a new fact must be added to `FACTS` or `METRICS` here and, if it is a word, given a closed list in `CHOICES`. A new event type needs a label in `LABELS` and a place in `select`. `tour.json` must be what the committed recording builds (drift test), so re-record, rebuild and commit together. A new meta tool, status, error type or demo act fails the copied-list test until this script is updated.
* P20 for P21. Work item 9 should start by re-recording with Docker (`toolforge images build`, then `python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/main/`, `pytest tests/test_demo_recording.py`, `python scripts/build_tour_data.py`, `python scripts/build_site_data.py`). Then read steps `second_opinion`, `trap` and `caught`: with a real sandbox they should show G6 running and the decoy catch (G5 also needs a challenger model). Update the tour's line in `docs/KNOWN_LIMITATIONS.md` and the last paragraph of "What the page says about its source" in `docs/SITE.md` to match. The browser tests read the committed `tour.json`, and `test_a_stand_in_recording_says_so_and_claims_no_decoy_catch` skips itself for an isolated recording.
* P20 not run: no recording with Docker (image pulls were blocked), `pytest -m docker`, `-m attacks` and `-m llm`, and no GitHub Pages deployment. The tour was checked in headless Chromium only; Firefox, Safari, a screen reader and a real phone were not tried.
* P19 for P20. `site/` is `site.json` (title, tagline, `repo_url` left empty on purpose, `allowed_external`, `quickstart`, `capabilities`, `features`), `src/index.html` (the skeleton) with one file per section in `src/partials/`, `src/styles.css`, `src/app.js`, `src/assets/` (logo, favicon, architecture diagram, gate icons) and `data/results.json`. `scripts/build_site.py` fills `{{placeholders}}` (list in its docstring and `docs/SITE.md`); the tests are `tests/test_site_{build,html,offline,data}.py`, `tests/browser/test_site_browser.py` and the shared `tests/site_helpers.py`. `build()` copies exactly `index.html`, `styles.css`, `app.js`, `assets/favicon.svg` and `data/results.json` to `site/dist` (`test_the_build_writes_the_expected_files` names that set: P20 extends both). `partials/tour.html` is a written three-step summary plus `{{demo_meta}}` and says the interactive tour is planned; P20 replaces it, keeps `id="tour"` and the hero's "Take the 2-minute tour" link, and flips the "Interactive guided tour" feature in `site.json` from `planned` to `built` with `patches: ["P20"]` (the build refuses a `built` chip whose patch is not ticked here).
* P19 rules that P20 inherits. The page's CSP is `default-src 'self'` and the browser tests run under it: no inline script, style or event handler, no `eval`; Playwright's string `page.wait_for_function` is blocked by it, so use `expect(...)`. A number from a measurement is written only through `fig()` (`<span class="fig">`) from `results.json`, and a missing one is the hatched `NOT_MEASURED`, never zero. `dist` stays under 400 KB without the social image. Links are relative. Colour comes from tokens in `styles.css`; a test checks AA contrast for both themes.
* P19 for P21. One manual step: repository Settings, Pages, Source: GitHub Actions. `site.json:repo_url` is empty so the address comes from `GITHUB_REPOSITORY` in the workflow; `--release` refuses a placeholder. The committed `site/data/results.json` holds no attack, benchmark or context figure (none is committed); regenerate it with `python scripts/build_site_data.py` after real runs. The recording it points to was made with the stand-in sandbox (`isolated: false`) and the page says so. The social image needs a converter (the workflow installs `librsvg2-bin`); without one the build warns and the page uses a plain share card. The Compose startup is still untried (P18). `pages.yml` pins `configure-pages` v6.0.0, `upload-pages-artifact` v5.0.0 and `deploy-pages` v5.0.1 by commit SHA: check for newer versions at release.
* P19 test environment. The browser tests need Playwright and its Chromium (`pip install playwright && playwright install chromium`) and are skipped without them. `tests/test_bench_harness.py::test_safety_counts_come_from_the_runs_ledgers` now takes the `minilocker` fixture and is skipped, not failed, in a checkout whose `third_party/minilocker` is empty (it writes real MiniLocker ledgers); the submodule is fetched by `git submodule update --init third_party/minilocker`.
* P18 files for P19 and P20. `demo/recordings/main/` is three files: `recording.json` (`schema`, `recorded_at`, `toolforge_version`, `seed`, `mode` replay|live, `agent` scripted|model, `sandbox` docker|stand-in, `isolated`, `g5`, per-act checker verdicts, event and ledger counts, a `note`), `events.json` (`events`: each `{t, act, ledger, tool, type, facts}`, `t` in seconds from the first event, `facts` only the keys of `FACTS`, `METRICS` and `PERMISSIONS` in `demo/recording.py`, nothing free-text) and `transcript.json` (per act: the agent's calls as names, sizes and 12-character argument hashes, never text). **The committed one was made with the stand-in sandbox** (`isolated: false`, G5 and G6 skipped, no decoy event): the tour must say so when `isolated` is false, and a recording made with Docker should replace it (`python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/main/`, then `pytest tests/test_demo_recording.py`). `demo.recording.validate(dir)` is the check the test runs; use it before a site build reads a recording.
* P18 for P19. `docs/KNOWN_LIMITATIONS.md` lines are `* **Short title.** One to three sentences.` (the test `tests/test_docs_p18.py` enforces it). `docs/PROJECT_SUMMARY.md` is under 400 words. `docs/assets/cover.svg` is 1200 x 630 and `python scripts/render_cover.py` makes `cover.png` (no dependency of its own; it prints the one-liners when no converter is installed). `docs/slides.md` is generated: change `scripts/build_slides.py`, not the file. The README has no site link yet; add it where the "Status" line used to be, in the first paragraph.
* P18 not run, on purpose: nothing was run against a real Docker daemon (none was available), so these are untested and the first thing to do on a machine with Docker is: `make demo-up` and check both containers are healthy and that `docker compose logs docker-proxy` shows no denied call during a `forge_submit`; call `forge_run` through the MCP URL (opens a sandbox through the proxy); approve a tool with a network host and run it (needs `TOOLFORGE_EGRESS_PROXY_IMAGE`, `egress.proxy` starting from the control image); `python demo/run_demo.py --replay demo/transcripts/` with Docker (act 3's second attempt should fail G6 with a decoy event; if G6 passes it, the demo's act 3 is wrong and the quiet package needs a different read); then re-record `main`. Also `docker compose config` (the compose test skips without Docker). The compose file lists the Docker API families by guess from MiniLocker's own pattern plus what the adapter calls; a denied call would show as a 403 in the proxy log and the fix is one more `1` in `docker-compose.yml`.
* P18 note on the benchmark's operator: `bench.harness.ScriptedOperator` promotes through `operator_actions.promote` without a ledger factory, so a benchmark run leaves no `operator.jsonl` and the dashboard shows no promotion event for it. The demo's `DemoOperator` passes `cli_run.make_ledger`. A later patch may give `ScriptedOperator` the same; P18 left it alone.
* P18 settings: `egress_proxy_image` / `TOOLFORGE_EGRESS_PROXY_IMAGE` (in `.env.example` and `docs/clients.md`). The compose file sets it to `toolforge-control:local`.

* P16 interfaces. `bench/` is run from a checkout and is not installed (`pyproject.toml` adds `pythonpath = ["."]` for pytest only). `bench/tasks/` (twelve modules; `Task`, `Instance`,
  `Check`, `TASKS`, `get`, `task_ids`, `prompt_for`, `tasks_markdown`; `docs/BENCHMARK.md` embeds the table between `tasks:begin/end`, drift-tested), `bench/agent.py` (`Agent`, `Budget`,
  `AgentResult`, `ToolSpec`, `ToolReply`, `McpBackend`, `PythonBackend`, `run_python_package`, `SYSTEM_PROMPT`, `prompt_hash`), `bench/harness.py` (`Options`, `Lab`, `RealLab`, `ScriptedOperator`,
  `OperatedBackend`, `FixtureServer`, `run_one`, `run`, `load_runs`, `forge_metrics`, `safety_counts`, `leaks_in`), `bench/stats.py` (`wilson`, `median`, `total`, `rate`, `overlaps`,
  `success_block`), `bench/report.py` (`summarise`, `attack_lab`, `markdown`, `write`, `load_context`, `load_attacks`, `store_attacks`), `bench/context.py` (`measure`, `Counter`,
  `EndpointCounter`), `bench/synthetic.py`. CLI: `python -m bench [--config] [--tasks] [--runs] [--seed] [--out] [--steps] [--max-tokens] [--max-seconds] [--fixture-host] [--fixture-bind]
  [--fixture-port] [--keep-homes] [--with-context] [--attacks FILE]`, `python -m bench context`, `python -m bench report --out DIR [--attacks FILE]`, `python -m bench tasks`.
* P16 files for P18 and P19. A run directory (`runs/bench/<UTC timestamp>/` by default) holds `runs/<config>/<task>/run-<n>.json`, `ledgers/...`, `summary.json`, `report.md`, and
  `context.json` / `attacks.json` when given. `summary.json` (`schema` 1): `generated_at`, `date`, `model`, `prompt_hash`, `runs_total`, `options`, `operator`,
  `configs.<config>.{overall,tiers.<tier>,tasks.<task>}` (each `runs`, `successes`, `success_rate`, `wilson_95`, `not_measured`, medians, `outcomes`, `stop_reasons`, `forge`, `safety`),
  `comparisons[]` (`difference_supported` only when the intervals do not overlap), `attack_lab` (`containment_rate`, `wilson_95`, verdict counts, `docker`, `rule`, `note`) and `context`
  (`null` when not run). Not measured is `null`, never 0. The website should read this file as is; the context figures may only be shown with their `method` label.
* P16 and P17. The dashboard's Benchmark page now draws `summary.json` (`static/app.js` `benchView`, chosen when a file has `configs` and `attack_lab`); the paths in
  `dashboard/results.py` were already right. `toolforge bench` in the dashboard's text was corrected to `python -m bench`. Checked once in Chromium with a generated summary.
* P16 behaviour. Tool calling is in the prompt (one JSON object per reply) through `toolforge.llm.client`, with the same prompt, budgets and model in every configuration. The baseline is
  `run_python` through the same `ToolRunner` and sandbox, no network. Forge runs get a fixed scripted operator (promote verified and network-free; approve only the fixture host),
  recorded per run. The fixture tasks are `not_measured` without `--fixture-host`; warm runs of tasks without a sibling are `not_measured`. Builder tokens (`forge_build`, `forge_repair`
  `usage`) are reported apart from agent tokens and are `null` when the provider gave none. The Attack Lab line is contained / (contained + breached + inconclusive) from P15's file.
* P16 not run: no Docker and no model endpoint were available, so **no benchmark run has been made**: not the acceptance run (`python -m bench --config baseline --tasks log_stats
  --runs 2`), not the fixture tasks through the egress proxy, and not the `RealLab` stdio path to `toolforge serve` with a real model. Run for real: the context measurement
  (estimate; figures in `docs/BENCHMARK.md`), every checker and generator, the agent loop with a scripted model, `McpBackend` against the real server in memory, `PythonBackend` through the
  real `ToolRunner` over the local stand-in runner, and the scripted operator against the real registry, `promote` and `decide`. What the first real run may show: whether the model keeps to
  one JSON object per reply (two bad replies end a run as `bad_reply`), whether 20 tool calls are enough for a build plus a repair, and which host name reaches the fixture server from
  the sandbox (`--fixture-bind` and `--fixture-host`).

* P17 interfaces. `dashboard/app.py` (`build_dashboard(settings, *, registry=, runs_dir=, ledger_factory=, event_interval_s=, session_close_wait_s=) -> Dashboard` with `.app` (FastAPI), `.guarded` (the
  `NetGuard` to serve) and `.close()`; `serve`, `describe_listener`, `status_for`), `dashboard/views.py` (read-side JSON), `dashboard/events.py` (`EventFeed.cursor/poll`, `summarize_event`),
  `dashboard/results.py` (`stored_results(runs_dir, "attacks"|"bench", name=None)`), `static/{index.html,app.js,style.css}`; `cli_dashboard.py` behind `toolforge dashboard [--host] [--port] [--runs-dir]`;
  `Registry.recent_calls/calls_after/last_call_id` and `CallRow`; `cli_sessions.close_one`; `NetGuard(token_paths=, env_var=, what=)`; settings `dashboard_allowed_hosts/origins`. Page: `docs/dashboard.md`.
* P17 for P15 and P16: the dashboard reads `<runs>/attacks/*.json` (what `toolforge attacks run --json` writes; drawn from its real fields) and `<runs>/bench/<run>/summary.json`, and runs nothing. P16 has
  fixed its format and drawn it (see the P16 bullets above).
* P17 and P14b: the pages show `kind`, `permissions.browser` and the steps of a browser plan; the approval page shows the plan steps the request carries (the P14b note asked for this).
* P17 for P18 and P19: `docs/dashboard.md` is the dashboard's page; the README has a short section. The dashboard page has not been exercised in a real browser (see its Limits); P19 and P20 own browser tests.
* P17 not run: `toolforge dashboard` in a real browser, and the Docker-dependent paths (approving a version with live examples, closing an orphaned session's container). The decision to follow the
  patch file over the P09 note about `decide` is in `DEVIATIONS.md`.
* P15 interfaces. `attacks/catalog.py` (`Attack`, `Check`, `evaluate`, `verdict_of`, `ATTACKS`, `get_attack`, `attacks_markdown`, `package`: the hostile packages are built here as strings) and `attacks/runner.py` (`Lab`, `run_one`, `run_attacks`,
  `render_table`, `write_json`, `exit_code`, `docker_problem`) behind `toolforge attacks list [--markdown]` and `toolforge attacks run [--only ID]... [--json] [--deadline S]` (`cli_attacks.py`); `docs/ATTACKS.md` embeds the generated table between
  `attacks:begin/end` markers (drift-tested); the `attacks` marker suite is `tests/attacks/test_attacks.py`; `--json` writes `runs/attacks/attacks-<time>.json` (P18 and P20 can read it as evidence). No new setting, ledger event or registry column. Written before S01, P14b and S02 landed, then brought up to date: seven more attacks (`plan_host_escape`, `plan_vocabulary`, `plan_page_text`, `bundle_forgery`, `bundle_stranger`, `chain_laundering`, `chain_ref_abuse`) attack the plan validator, signed bundles and chains, all without Docker; `Lab` gained `stranger()` (another person's data directory that signs and exports bundles), `trust`, `approve`, `quarantine`, `chain_service`, `mcp_tool_names_for(chain_enabled)`, `import_bundle`, `created_by_of`, `home_snapshot` and `scratch`; `approval_over_mcp` now lists the server with chains on and also forbids import, export, trust and bundle in a tool name.
* P15 behaviour. Verdicts are `contained`, `breached`, `inconclusive` and `skipped`; an attack's checks are `pass`, `fail`, `unproven` or `skip`, and nothing is `contained` unless an attack's own check passed and none failed or is unproven. Evidence is the ledger, the registry and Docker's
  own figures (`sandbox.created` hardening, `sandbox.audit` stats and file-system audit), never the tool's output. Every attack also gets three run-wide checks (host canary token absent from the data directory, every ledger verifies, every sandbox destroyed and no container left behind).
  Exit code 1 on any breach, 2 when something is inconclusive or skipped, else 0. `SeedGate` puts *victim* tools in the registry with scripted gates and no verdict rests on one. The lab's settings configure no model, so no hostile package is ever sent to a real LLM (G5 uses a scripted `FakeLLM`).
* P15 not run: no Docker was available where the patch was written, so the twenty-seven-attack run against real sandboxes (`pytest -m attacks`, `toolforge attacks run`) has **not** been done and the Docker attacks are untested against a container. Run for real: the thirteen attacks that need no sandbox (all `contained`, and each turns `breached` when its defence
  is broken in `tests/test_attacks_drivers.py`), the real G1 over every hostile package, and the verdict logic with synthetic evidence for all twenty-seven. What the first Docker run may show: counters are sampled (`peak_pids`, `peak_memory_bytes`), so `fork_bomb` and `memory_bomb` can come out `inconclusive` if a spike is missed; `disk_fill` may end by OOM
  rather than ENOSPC (it is given 512 MB to avoid that); `output_flood` depends on whether the harness caps printed output; `sdist_exec` needs the build gate's access to PyPI and is `inconclusive` without it. An `inconclusive` there is the lab being honest, not a failure; a `breached` is a real finding.
* Fix P15 (signing-key test). `tests/test_provenance_bundle.py::test_the_signing_key_is_used_by_export_and_nothing_else_in_the_package` (S01) failed on `main` once P15 landed, because the attack lab's `Stranger` (`attacks/runner.py`) loads and signs with a key. The two patches were
  written in parallel. `Stranger` is somebody else's ToolForge with its own throwaway key in a temporary directory, never the operator's, so no code was changed. The test now names the callers by class and method: `export_bundle` and `attest.sign` as before, plus exactly `Stranger.__init__`
  (`load_private_key`), `Stranger.resign` and `Stranger.claim_to_be` (`sign`). A call from any other method, including another `Stranger` method, still fails it (checked by adding one). New test `test_the_attack_labs_stranger_never_holds_the_operators_key`: its home is outside the
  operator's and its fingerprint and public key differ. Nothing else changed. If the lab ever needs a key from the operator's home, that is a change to this rule, not to the test.
* P14b interfaces. `adapters/browser_plan.py` (`STEPS` and `LIMITS` are the one vocabulary; `parse_plan`, `validate_plan`, `template_args`, `render_plan`, `run_steps`, `host_of`, `PlanError`, `PlanFailure`);
  `adapters/browser_fixture.py` (`load_fixtures`, `FixtureBrowser`); `sandbox/browser_adapter.py` (`PlanRunner`, `BrowserAdapter`, `PlanOutcome`, `BrowserAudit`, `browser_for`; in `ALLOWED_IMPORTERS`); `verify/plan_gates.py`
  (`is_plan`, `dry_run`, `limits`, `audit`, `HOSTILE_VALUES`); `GateContext.browser`; `approvals.plan_lines`. Manifest: `kind: "browser_plan"`, `entrypoint: "plan.json"`, `permissions.browser: true`; the permission view has `browser` only when true.
  Recipe `browser-plan` (in `RECIPE_KINDS`), pack `examples/toolpacks/browser_headlines/`, pages `docs/browser-plans.md` and `docs/gui-kit.md` (documentation only: no GUI kit exists).
* P14b behaviour. Whatever lists, runs or measures tools now meets a kind with no code: G5 is skipped, so a plan is never `verified`; G6 is hardening only and skipped without the browser image; `toolforge run --dev-unverified` and direct tool exposure do not run plans.
  P15 can attack the plan validator, the templates, page text and the proxy's host rules. P16's browser task needs the real browser image and a real site, or says it was not measured. P17 should show `kind` and the plan steps.
* P14b not run here: loading a real page through the egress proxy; MiniLocker's own `minilocker-browser` image and the real runtime and proxy images (Docker Hub could not be reached, so the audit, the proxy block and the `browser_headlines` golden test were run with stand-in images made from this machine's files); every other `docker` test (the other golden packs among them); the `llm` tests.
* Fix P14b (real-page test). Run by the operator on 2026-11-27 with the images built on their machine, which updates the bullet above: `tests/docker/test_golden_packs.py` 8 passed; `pytest -q` 3176 passed, 4 skipped (no model settings, `chess` and `gymnasium` absent), 1 failed. The failure was `test_a_plan_reads_a_real_page_through_the_proxy`: the plan loaded `https://example.com` through the proxy, but the text did not hold "Example Domain". The test now asks only for the word "domain". Not run since the change, so the three assertions after that line have not run against a real page. The page's own text asks not to be relied on for testing; a page this project controls would be steadier. The same run printed one `PytestUnraisableExceptionWarning` (urllib3, Python 3.14) in `test_context_budget`; its source has not been traced.
* P14b not handled (pick up where it fits): the builder prompt is written for Python tools and `forge_repair` without a `package` asks it even for a plan (refuse, or teach it); `toolforge doctor` does not check the browser image; a Blast Radius report calls blocked third-party page resources a violation; a skipped G6 needs a new version to be measured; no command shows a package's files; `output_path` files are still discarded; an exported plan's attestation records the code runtime image (if any), not the browser image.

* S01 interfaces. `provenance/keys.py` (`init_keys`, `load_private_key`, `own_public_key`, `trust_key`, `untrust_key`, `list_trusted`, `trust_of`, `fingerprint`, `parse_public_key`),
  `provenance/attest.py` (`Attestation`, `canonical_json`, `gate_statements`, `sign`, `parse`, `check_signature`), `provenance/bundle.py` (`read_members`, `verify_members`,
  `verify_bundle`, `import_bundle(path, settings, registry, submit)`, `export_bundle(settings, registry, ref, out, force=, now=, image_lookup=)`, `VerifiedBundle`, `Signer`,
  `ImportResult`, `ExportResult`, `MEMBER_LIMITS`), `cli_provenance.py` (`toolforge keys init|show|trust|untrust|list`, `export`, `verify-bundle`, `import`), `ProvenanceError` in `errors.py`.
  `server.tools.OPERATOR_ONLY` names the S01 functions. Page: `docs/PROVENANCE.md` (its error table is checked against the code). Test helpers: `tests/provenance_helpers.py` builds real
  verified versions with real ledger files and scripted gates, and re-packs or re-signs bundles so one check at a time can be made to fail.
* S01 behaviour. A signature never changes a status, skips a gate or promotes: import is `Forge.submit_package` with `created_by = "import:<fingerprint>"`. The bundle's gate outcomes are shown as
  information only. `attestation.json` must be canonical JSON byte for byte. An unknown signer verifies and is shown as "unsigned by anyone you trust". There is no key expiry or revocation, only
  `keys untrust` on each receiver; the ledger is a hash chain, not a witness, so a signer can write a consistent history that never ran (all of this is in `docs/PROVENANCE.md`). P21 should include
  `tests/test_provenance_*.py` and `docs/PROVENANCE.md` in its checks.
* S01 not run: exporting and importing a tool that was verified through real Docker containers, and `toolforge export csv_summary@1.0.0` itself (the CLI tests round-trip `word_count` through the stand-in
  sandbox with the real harness and the real MiniLocker ledger; every other test uses scripted gates). Not measured: behaviour on Windows without WSL2 (file modes cannot be enforced).
* S02 interfaces (optional patch, applies on top of P14b). `services/chain.py`: `ChainRunner(settings, registry, runner, *, ledger_factory, clock=, new_id=).run(steps) -> ChainResult`, `parse_request`, `parse_ref`, `substitute`,
  `HARD_MAX_STEPS` (8). `ToolRunner.run(..., chain_id=)` only adds `chain_id` to that call's `forge.call.started` / `forge.call.finished`. `server.tools`: `forge_chain` is the last entry of `META_TOOLS`, `OPTIONAL_TOOLS`,
  `enabled_tools(settings)` (what a server offers), `chain_view(result)` (the fenced response), `ToolService.chain(steps)`; `register_tools(server, service, settings=None)` skips an optional tool whose setting is off. Settings
  `chain_enabled` (false), `chain_max_steps` (5, 1 to 8), `chain_total_seconds` (60). `fences.result_text` is public. Page: `docs/CHAINS.md`. Test rig: `tests/chain_helpers.py` (`ChainRig`, `step`, `ref`).
* S02 behaviour. Tools are pinned at the chain's start (a version deprecated in the middle still runs, a quarantined one fails its step); every step is checked for eligibility (no network, secrets, files, browser, session; passed G2; stored blob)
  before step 1; a failing step stops the chain and the rest are `skipped`. The chain ledger (`ledgers/chains/<id>.jsonl`) holds ids, statuses and output hashes only. Anything that lists tools must use `enabled_tools(settings)`, not `META_TOOLS`,
  to know what a server offers. There is no per-caller rate limiter in the repository, so none was changed (see DEVIATIONS).
* S02 not run: a chain of real tools in real containers (all chain tests use a scripted sandbox over the real registry and runner), `pytest -m docker`, and P16's chain-versus-separate-calls comparison (P16 is not built; "not measured").
* P14a interfaces. `images/runtime/{Dockerfile,requirements.txt}`; `cli_images.py` (`build_image`, `build_command`, `TAG`, `register`) behind `toolforge images build [--tag] [--context] [--no-cache]`; `config.RUNTIME_IMAGE` (`toolforge-runtime:py312`, now the default `runtime_image`);
  `sandbox/image_info.py::image_id(image)` (never raises, never pulls); `ToolRunner(..., image_id=)`; `forge.call.started` and `forge.session.opened` carry `image` and `image_id`; `forge_runtime_info` has `image_id`. `adapters.RECIPE_KINDS` (the one list; P14b adds `browser-plan`)
  and four recipes (`sql`, `file-format`, `http-api`, `game-state`), each with a complete example package in fenced blocks (` ```json manifest `, ` ```python tool.py `, ` ```python tests/... `) that `tests/test_recipes.py` runs through the real gates. Seven packs in `examples/toolpacks/`.
  Page: `docs/runtime-image.md` (its library table is checked against `requirements.txt`). `scripts/check_guide.py` now accepts a patch id with a letter suffix and treats a bare `P14` as P14a and P14b together.
* P14a behaviour. Docker tests that run tools skip when `toolforge-runtime:py312` is not built (`tests/conftest.py`); build it with `toolforge images build`. A manifest example can only carry text, so `sqlite_query` takes a `.db` file or a `.sql` dump and its example uses the dump.
  A tool with `permissions.network` needs an input-schema property with no `pattern` or G4 is skipped and the version cannot be approved. P14b needs to know: `permissions.browser` is still rejected as reserved, there is no `browser_plan` kind, and `output_path` files are still discarded (not in P14a's scope).
* Fix P14a (chess wheel). The first real `toolforge images build` failed: `chess==1.11.2` is source-only on PyPI and the image installs with `--only-binary=:all:`, so it is pinned to 1.10.0 (the newest release with a wheel). Every pin was then checked against PyPI for a wheel and the exact install command was repeated in a fresh Python 3.12 venv (`pip check` clean, all libraries import). `build_image` now looks up `shutil.which` when it runs, so `test_the_subcommand_is_registered_...` no longer starts a real `docker build` on a machine that has Docker.
* P14a not run: every `docker` test (`tests/docker/test_runtime_image.py`, `tests/docker/test_golden_packs.py`, and the existing docker tests, which now need the built image), a successful `docker build` in a container (the first attempt failed on the chess pin and is fixed above; the install was reproduced outside Docker only), and the seven packs inside a container. The stand-in sandbox is not isolated.

* P13 interfaces. `registry/sessions.py` (plain storage; `new_session_id`, `split_session_id`, `secret_matches`, `SessionRow`, ...) behind `Registry.open_session`, `get_session`,
  `list_sessions`, `count_open_sessions`, `open_sessions_of`, `touch_session`, `note_session_rewind`, `session_heartbeat`, `request_session_close`, `close_session`; migration 4 adds
  `sessions`. `services/sessions.py::SessionManager(tools, clock=, sleep=, reaper=)` with `call`, `resolve`, `close`, `rewind`, `describe`, `close_for_version`, `sweep`, `shutdown`;
  `ToolRunner.sessions` is one (`ToolRunner(..., session_clock=, session_sleep=, session_reaper=)`), and `ToolRunner.run(..., session_id=)` dispatches `kind: "session"` to it.
  Harness: `state` protocol, `--state`, batch `"session": true`; `verify/sandboxrun.py` has `is_session`, `session_request`, `reset_state`. MCP: `forge_run(session_id)`,
  `forge_session_close|rewind|list`. CLI `toolforge sessions list|close`. Settings `session_ttl_s`, `session_max_s`, `max_sessions`. `report/blast_radius.session_report`. Page: `docs/sessions.md`.
  P14's adapter kits and recipes can use `state` (a seed plus an action list) for engines that cannot be stored.
* P13 behaviour. A failed call is not a step; a dead sandbox is recovered to the last step. Session ids are capabilities (public half in lists, secret half hashed). Quarantine closes
  a version's sessions, deprecation does not. `doctor --clean` leaves containers of live sessions alone. P17's dashboard should read `registry.list_sessions()`, never ledgers.
* P13 not run: every `docker` test (`tests/docker/test_session_containers.py`: counter 1,2,3, rewind, container removal on close and on TTL), and real MiniLocker `freeze`/`thaw`/rewind and
  the reaper against real containers. Everything else ran against the real harness through a stand-in sandbox that is not isolated, so snapshots there are copies of a directory.
* P12 interfaces. `registry/search.py`: `RANKING` (the one constant block), `rank`, `confidence_of`, `text_confidence`, `coverage_of`, `smoothed_success`; `Hit` gained
  `confidence`, `why`, `coverage`; `Stats` gained `recent_ok`. `registry/health.py` (plain storage behind `Registry.record_failure`, `failures`, `regression_inputs`,
  `recent_outcomes`, `add_problem_report`, `problem_reports`, `count_problem_reports`, `begin_repair`, `finish_repair`, `repair_state`, `repairs`); migration 3 adds
  `regression_cases`, `problem_reports`, `repairs`. `services/failures.py` (`fingerprint`, `stored_input`, `quarantine_verdict`, `capture`, `judge`; the runner calls it
  after every recorded call). `services/repair.py`: `Repairer(settings, registry, forge, *, client, ledger_factory=, runtime_facts=, clock=, new_id=).repair(name, problem,
  package=None, *, on_progress=) -> RepairResult`. `server/signature.py::build_signature(manifest)`, `server/budget.py` (`BUDGET`, `fit_describe`, `estimate_tokens`).
  `operator_actions.rollback(settings, registry, name, to_version, reason, *, ledger_factory=)`. CLI `toolforge rollback <name> --to <v> --reason ...` and `toolforge problems
  [name[@version]]` (`cli_health.py`). MCP: `forge_repair`, `forge_report_problem`; `forge_describe` has `signature`; `forge_find` has `why` and a `hint`; `forge_status` has
  `problem_reports` and `needs_operator`. Settings: `repair_max_per_hour` (3), `store_failed_inputs` (false). `GateContext.regression_cases`, `Pipeline.run(...,
  regression_cases=)`, `VerificationReport.similar_to`. Operator page: `docs/repair.md`.
* P12 behaviour. `Forge` hands every new version of an existing tool the stored inputs that broke it (G3 runs them through `--batch`; a result or a `ToolError` passes).
  Failure capture is the shape only unless `store_failed_inputs` is on. A rate above 40% over at least 10 of the last 20 non-`ToolError` calls quarantines the active
  version (actor `forge`); the window restarts when a version changes status. Repair never promotes: the old version stays active. P13 (sessions) must not change
  `calls` rows in a way that breaks `recent_outcomes`, which joins `calls.ts` to `versions.updated_at`.
* P12 not run: every `docker` test, `pytest -m llm`, and `forge_repair` with a real model (all repair tests use `FakeLLM` with scripted gates; how often a real model repairs a
  tool is not measured, P16). The G3 regression run was proven with a scripted sandbox, not a container. The context budget was measured with 10 and 60 reference tools,
  not 200 (building 200 verified tools took 100 s); `find` and `tools/list` do not depend on the registry's size, which the test shows.

* P11 interfaces. `services/builder.py`: `Builder(settings, registry, forge, *, client, ledger_factory=, runtime_facts=, confidence=, clock=, new_id=)` and
  `.build(goal, *, examples=, sample_files=, constraints=, on_progress=) -> BuildResult` (`ok`, `reused`, `stop_reason`, `tool`, `version`, `status`, `rounds`,
  `attempts`, `usage`, `report`, `permissions`, `failure_summary`, `next_action`, `name_adjusted`, `warnings`; `.as_dict()` and `.text()`). `BuildRequest.parse` checks
  and secret-scans the four inputs. `system_prompt`, `draft_prompt`, `revise_prompt`, `sandbox_section` and `bump_patch` are public. `STOP_REASONS` lists why a build ends.
  `server.tools.runtime_facts(settings, probe=None)` is the one definition of what `forge_runtime_info` shows; `build_view(result)` is how an agent sees a build.
  `ToolService.build(...)` makes its `Builder` on first use (`build_server(builder=)` lets a test pass one). CLI: `toolforge build "<goal>" [--examples F] [--sample F ...]
  [--constraints T] [--json] [--quiet]`. Settings: `builder_max_rounds` (4), `builder_token_budget` (60000). Doctor has a `builder model` check. Operator page: `docs/builder.md`.
* P11 behaviour. A build searches active tools first (`confidence == "high"` from `forge_find`'s measure) and needs no model for that. Otherwise the model's package goes
  through `Forge.submit` as author `builder`; the loop sets `name`, `version` (0.1.0, then the next patch) and `parent_version`. A taken name becomes `name_2`. Only the first
  failed gate's id, name, summary and hints go back, with the previous package (a file with secret-shaped text is named, not echoed). A build stops on `verified`, `reused`,
  `max_rounds`, `stuck` (same gate and summary twice, or two unusable replies), `token_budget` (checked before every round after the first, with the next prompt's size
  added), `llm_unavailable`, `llm_refused`, `busy` or `name_conflict`. Rejected versions stay in the registry. A sandbox failure is raised after the ledger event
  (`outcome: aborted`) is written, and leaves its version `verifying`. Each build writes `forge.build.finished` to `ledgers/_builds/<build id>.jsonl` (ids, hashes, counts; never
  the goal, a prompt or code). Without a model only the reuse step works. The builder cannot promote, approve, release or roll back (a test reads its source).
* P11 for later patches. P12's self-repair can call `Builder.build` with the failure as the goal and `constraints`, or reuse `revise_prompt`; its versions must follow
  `bump_patch`. P16's benchmark should read `BuildResult.usage` and `.attempts` for rounds and tokens, and must label `budget_basis: estimated` figures as estimates. The
  builder shares one rate limiter per process for the builder model (`llm_max_rpm`); a free tier that keeps answering 429 ends a build as `llm_unavailable`.
* P11 not run: `pytest -m llm tests/test_builder_live.py` (no endpoint where the patch was written), every `docker` test, and `toolforge build` with a real model and
  Docker. The loop is proven with `FakeLLM`, a real registry and scripted gates; `toolforge build` with the real gates G1 to G4 through the local stand-in sandbox. How often
  a real model writes a passing tool, and at what cost, is not measured (P16). MiniLocker was not present where the patch was written, so the injection scan reported "not measured".

* P10 interfaces. `toolforge.llm.client`: `LLMClient(base_url, model, api_key, timeout_s=, limiter=, transport=, json_mode="prompt"|"native", wall_budget_s=, clock=, sleep=, rng=)`,
  `client_from_settings(settings, "builder" | "challenger")` (None when no endpoint and model are set; the challenger falls back to the builder's values),
  `JSONChatClient.complete_json(system, user, schema_hint=, check=) -> JsonReply(data, model, prompt_tokens, completion_tokens, latency_ms, requests, nudged)`,
  `FakeLLM(replies, model=)` (strings, dicts or exception instances; goes through the same scan, parse and nudge; keeps `.requests`), `RateLimiter`,
  `shared_limiter` / `reset_shared_limiters`, `parse_json_object`, `refuse_secrets`, `Transport` (`post(url, headers, body, timeout_s)`) and `UrllibTransport`.
  New errors in `errors.py`: `LLMError`, `LLMUnavailable` (not configured, unreachable, rate limited past the budget, key refused), `LLMRefused` (secret-shaped prompt),
  `LLMBadReply` (two unusable replies). A gate turns the last three into `skipped`. P11 (builder) and P16 (benchmark agent) should use `client_from_settings(settings,
  "builder")` and `complete_json`; ask for plain text with a small `chat` wrapper rather than a second HTTP path. Everything a model returns is data: validate it with pydantic,
  never run it. `verify/checks_dsl.py` is the closed check vocabulary (`parse_check`, `evaluate`, `describe`, `CHECK_KINDS`; the prompt file lists the same eight, a test checks it).
  `verify/g5_challenger.py::ChallengerGate(client=None, min_cases=8, host_cases=12)` is registered in `GATE_CLASS_PATHS`; `build_user_prompt` and `system_prompt` are public.
  Settings added: `llm_max_rpm`, `challenger_max_rpm`, `llm_timeout_s` (`Settings.effective_challenger_max_rpm`, `.challenger_shares_builder_model`). Doctor has a `challenger model` check.
* P10 behaviour. No model, an outage, a refused key, two unusable replies or a secret-shaped prompt give G5 `skipped` (the version stays `verified_basic`; promote or approval
  still work). G5 blocks on: any crash, timeout, oversized or non-JSON result for a schema-valid case (host or challenger), a `high` confidence check that fails, any
  `permission_findings`, and fewer than `min_cases` usable challenger cases. `medium` failures and `risk_flags` are warnings in `detail["warnings"]`. The ledger gets model id,
  token counts and case counts through the gate's metrics only. The challenger sees no files for cases (it cannot supply inputs), so a tool that needs `inputs/` files is
  exercised only by the host's cases. Real calls to the batch mode use the unchanged harness. Not measured: how often the challenger finds a real defect (P16).
* P10 not run: `pytest -m llm` (no endpoint where the patch was written), every `docker` test, and `toolforge submit examples/toolpacks/csv_summary` with a real model.
  The urllib transport has been exercised only through mocks, never against a real server.

* Revision 4 additions (guide only, no code yet), picked up by the patch that owns them: **P12** adds `forge_describe.signature` (typed Python and TypeScript
  stubs built only from the schema; no author text in a code position) and a response size budget (`server/budget.py`, `tests/test_context_budget.py`);
  **P16** adds `python -m bench context` (tokens per `tools/list`, `forge_find` and `forge_describe` at 10, 100 and 1000 synthetic tools, labelled estimate or
  measured); **P19** shows those figures only when `results.json` has them; **S02** (optional) is `forge_chain`, a data-only chain of verified, network-free,
  secret-free tools. Nothing in P10 or P11 changes. See `docs/guide/REVISIONS.md`, Revision 4.

* P09 interfaces. `services/approvals.py` (everything about an approval except the decision, safe for the server: `open_request`, `deny`, `expire_overdue`,
  `permission_diff`, `render_text`, `request_path`; requests are rows in `approvals` and `approvals/<id>.json`), `verify/g7_approval.py::ApprovalGate`
  (registered in `GATE_CLASS_PATHS`, not in `PIPELINE_GATE_IDS`), `operator_actions.decide(settings, registry, approval_id, "approve"|"deny", reason,
  tool_runner_factory=, brokers=, ledger_factory=)`, CLI `toolforge approvals list|show|approve|deny`. `VerificationReport.approval` is `{id, state}` and `ok` is
  true for `pending_approval`. `SandboxSpec.broker`; `ToolRunner.run(..., record_stats=)`; the runner refuses network or secrets unless the version is active with a
  recorded approval for its `code_sha`, and a dev run gets neither. `EgressPool.acquire(hosts, services)`; `configured_broker_services` / `broker_service_hosts`
  read MiniLocker's `MINILOCKER_BROKER*` from the process environment. Blast Radius now has `runtime_sample.egress` and `.real_secret_audit`, and judges real-call
  egress against the hosts and services `forge.call.started` declared. `docs/approvals.md` is the operator's page.
* P09 limits that later patches inherit. The proxy has one allowlist and one broker for all tools (`docs/upstream/minilocker-per-sandbox-allowlist.md`); expiry of a
  pending request happens when someone looks (`approvals` commands, `forge_status`, `decide`); `tests/docker/test_egress.py` has not been run on a real daemon.
  P12's `rollback` must only target a version that was active before (so it already has an approval); P15's attack lab should try the undeclared-host and
  real-secret paths; P17's dashboard should call `approvals.render_text`/`expire_overdue` read-only and never `decide`.

* P08 interfaces. `verify/g6_containment.py::ContainmentGate` (G6, registered in `GATE_CLASS_PATHS`); `sandbox.base.decoy_violations` and `decoys_planted`
  (one definition shared by G6 and the runtime tripwire); `sandboxrun.open_sandbox(..., decoys=)`; `FakeRunner(events=[(type, payload)])` scripts
  `canary.*` and `egress.*` events. `report/blast_radius.py`: `generate(settings, name, version)`, `load_stored`, `summarize`, `missing_summary`,
  `render_text`, `render_html`; stored as `ledgers/<tool>@<version>/blast-radius.json` (no database column). `ledger.read_ledger`, `verify_chain`,
  `version_ledger_dir`, `OPERATOR_LEDGER`. `operator_actions.quarantine`, `release`, `release_context`; CLI `toolforge release|quarantine|report`;
  `forge_describe` returns `blast_radius`. `ToolRunner` quarantines the active version (actor `forge`) when a call touches a decoy and fails the call with
  `PolicyViolation`. Egress is in the report since P09 (G6 runs with the network closed, so egress there is `not applicable`, never `clean`).
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
