# Deviations

Where reality differed from the guide, newest first: what the guide said, what is true, what was done.

* 2026-11-27 (Fix P14b): the real-page test expected `https://example.com` to show "Example Domain". The visible text the browser
  container read on 2026-11-27 starts "This domain is for use in documentation examples" and does not hold that heading. The plan
  itself worked. The test now asks only for the word "domain" in the text; no source code changed.
* 2026-11-26 (S02): the guide puts the module in `src/toolforge/core/chain.py`. It is `src/toolforge/services/chain.py`, next to the runner it calls (see the
  P05, P06 and P11 entries on `core/`). It also names `ToolRunner.run(record, args)` alone; the runner gained one optional keyword, `chain_id`, which only adds
  `chain_id` to that call's `forge.call.started` and `forge.call.finished` events (nothing else changes, and a call without it writes exactly what it wrote before).
* 2026-11-26 (S02): the guide says "the existing per-caller rate limit counts every step". The repository has no per-caller rate limiter (`llm_max_rpm` limits calls
  to a model, not to tools). Nothing was invented: each step is an ordinary `ToolRunner.run`, so a limiter added there later counts every step, and
  `docs/CHAINS.md` says none exists today. The test proves one sandbox and one pair of call events per step instead.
* 2026-11-26 (S02): the guide says `chain_enabled=false` "makes `forge_chain` return a clear disabled error and the tool is not listed". Both hold, at two levels:
  the server does not register or list the tool (a client that calls it anyway gets the SDK's unknown-tool error), and `ToolService.chain` /
  `ChainRunner.run` raise `chain_disabled` with a hint. `META_TOOLS` still holds `forge_chain` (the table, the docs and the drift tests describe every tool the server
  can offer), and `tools.enabled_tools(settings)` is the list a server offers. Four existing tests that pinned the exact tool list (`test_server_tools`,
  `test_server_direct`, `test_server_http`, and the "since" map in `test_docs_clients`) now use `enabled_tools(Settings(chain_enabled=False))` / know `forge_chain` is `S02`.
* 2026-11-26 (S02): the guide says "if any step is flagged, `possible_injection` is true on the chain". The chain response follows `forge_run`: `flags` holds
  `"possible_injection"` (and each step has its own `flags`). `fences.result_text` was made public (it was `_result_text`) so `chain_view` can use it.
* 2026-11-26 (S02): a setting `chain_total_seconds` (default 60, 1 to 600) was added next to `chain_enabled` and `chain_max_steps` (default 5, 1 to 8), as the guide's
  text implies; all three are in `.env.example`.
* 2026-11-26 (S02): `tests/chain_helpers.py` (the rig) needed one change to run against this repository: its loose input schema now lists its properties with
  `additionalProperties: false`, because the package rules (P02) refuse `additionalProperties: true`. The chain module had one bug found by the tests: `_shown(key, 20)`
  asked `textsafety.sanitize` for a limit below its minimum (28) and raised `ValueError` for any step with an unknown field instead of a `bad_chain` refusal; the limit is now raised to the minimum.
* 2026-11-27 (P14b): the guide's pitfall holds: MiniLocker's browser worker cannot read an element by selector, only the page's visible text (cut at 12000 characters) and at most 40 interactive elements. So `extract_text` works on the visible text (`after`/`before` anchors, `lines`, `contains`, `skip`, `limit`) and `extract_attr` on that element list (`href`, `label`, `name`, `type`; matched on the label), not on a selector. `click` and `type` selectors are resolved by Playwright, so the recipe says "CSS, as Playwright reads it". The worker uses an input's current value as its label, so `extract_attr` skips password fields.
* 2026-11-27 (P14b): `{{arg}}` templating is narrower than the guide's wording: only a `goto` path, query or fragment (percent-encoded, `.` and `..` segments refused), `type.text`, and `after`, `before`, `contains`, `match`. Never a host or a selector, because a templated selector would let an argument choose the element, including a password field. "Never carry credentials" is enforced by a plan declaring no secrets and by a heuristic on the names in selectors and templated arguments; a heuristic cannot know what an element is.
* 2026-11-27 (P14b): the guide did not give the package layout. `entrypoint` is `plan.json`; the other files may only be `fixtures/*.json` and `.md`/`.txt` notes; no `secrets`, `inputs`, `requirements` or example `files`; at least one network host; there is no `tests/` folder (the examples are the tests); the result is `{"fields": {...}}`. The issue code `permissions.browser.reserved` is gone; `permissions.browser.kind` and the `plan.*` codes replace it.
* 2026-11-27 (P14b): G3 does not serve a fixture page. The examples run in-process against recorded pages written as JSON (`adapters/browser_fixture.py`), so no browser, server or page script is involved, and it shows that the plan matches the recording, not the live site. Stored inputs that broke an earlier version are not replayed (`regression_cases_not_run`).
* 2026-11-27 (P14b): G5 is `skipped` for a plan (the challenger calls code), so a plan is at best `verified_basic`. G6 audits the hardening of a browser container started with no network: the plan is not run and no decoy is planted. Without the browser image G6 is `skipped`, which does not by itself stop an approval, and it is not repeated for the same version.
* 2026-11-27 (P14b): `sandbox/browser_adapter.py` joins `ALLOWED_IMPORTERS` (the second place that reaches a MiniLocker sandbox). The browser image is MiniLocker's own, built by hand from the submodule; `toolforge images build` does not build it and `toolforge doctor` does not check it.
* 2026-11-27 (P14b): not in the scope list: an approval request for a plan lists its steps, read from the frozen package, and two extra limits (`services/approvals.py`). Without it an operator would approve hosts without a way to see what the plan clicks or types, because no command shows a package's files.
* 2026-11-27 (P14b): host control is only as fine as MiniLocker's proxy (one allowlist per process, subdomains pass). The plan's own check reads the page's address after each browser reply, so a redirect is caught after the page was fetched. Blocked third-party resources appear in a call's ledger, and a Blast Radius report that samples the call says `violation_seen`; the report's rules were not changed.
* 2026-11-27 (P14b): the reference pack `browser_headlines` uses a host that does not exist (`headlines.example.org`) and made-up pages, so it proves the gates and not a real site.
* 2026-11-26 (S01): the guide's attestation has `signer` (a fingerprint) and `signature` only. It also carries `signer_key`, the signer's public key, and the
  signature covers it. Without it a receiver could not check a stranger's signature at all, and the guide wants an unknown signer to be allowed and shown as
  "unsigned by anyone you trust". Trust is still decided only by the receiver's own key list, by the fingerprint computed from the key's bytes.
* 2026-11-26 (S01): the guide says the signature is over "the canonical JSON of the rest". `attestation.json` must also be that canonical JSON byte for byte
  (`attestation_not_canonical` otherwise). A signature over the parsed fields alone verified a file that had a repeated key, `"schema": true` or `1.0`, which
  another JSON reader could read differently; this was found by probing the first version of `attest.parse` and is now a test.
* 2026-11-26 (S01): `keys.py` created the `keys/` folder with the default mode when `keys trust` ran before `keys init`, although the module promises a 0700 folder.
  `_folder` now creates and tightens it to 0700 (the private key file was 0600 either way).
* 2026-11-26 (S01): scope additions the guide does not list: `src/toolforge/cli_provenance.py` (the commands, registered through `cli.REGISTRARS`),
  `ProvenanceError` in `errors.py`, six names added to `server.tools.OPERATOR_ONLY` (`export_bundle`, `import_bundle`, `load_private_key`, `init_keys`,
  `trust_key`, `untrust_key`), `tests/provenance_helpers.py`, `tests/test_docs_provenance.py` (the error-code table is checked against the code), and the
  README pointer. `cryptography==50.0.2` is a runtime dependency in `pyproject.toml` (the standard library cannot sign); it was already installed with `mcp`.
* 2026-11-26 (S01): the guide's `verify_events` is reached as `toolforge.ledger.verify_chain`, which calls MiniLocker's `verify_events` through the
  existing import boundary, so `provenance/` imports no MiniLocker module itself and `tests/test_import_boundary.py` is unchanged. A consequence worth knowing:
  `verify-bundle` needs the MiniLocker submodule, though not Docker.
* 2026-11-23 (P11): the guide puts the builder in `src/toolforge/core/builder.py`. It is `src/toolforge/services/builder.py`, next to the forge it
  calls (see the P05 and P06 entries on `core/`). The prompt is `src/toolforge/llm/prompts/builder.md`, as the guide says.
* 2026-11-23 (P11): scope additions the guide does not list: `src/toolforge/cli_build.py` (the command, registered through `cli.REGISTRARS`),
  `docs/builder.md`, a `builder model` check in `toolforge doctor` (it reads settings and contacts nothing), `server.tools.runtime_facts` and
  `build_view` (the facts that `forge_runtime_info` shows, now one function that the builder's prompt and the command also use, and the way an agent
  sees a build), `tests/builder_helpers.py`, and `Builder` as an optional argument of `build_server` so a test can pass a scripted one.
* 2026-11-23 (P11): the guide says the tool returns "the final `VerificationReport`". It returns the report under `report` inside a larger result
  (`ok`, `stop_reason`, `tool`, `version`, `status`, `rounds`, `attempts`, `usage`, `permissions`, `next_action`, `operator_command`), because a build can end
  without any report (a model outage, a refused request, a reply that was never a package) and the caller needs to know why in every case. A failed
  build is a result with `ok: false`, not a tool error; a bad request or a missing model is a tool error.
* 2026-11-23 (P11): `forge_build` is the only tool with `open_world_hint: true`, because it sends text the caller wrote to the operator's model provider.
  The other tools stay `false`.
* 2026-11-23 (P11): the stuck rule also covers the model twice returning something that is not a package, and a name that was never registered (its
  package was too invalid to store) may be used again by the next round. The loop, not the model, owns `name`, `version` and `parent_version` as the
  guide says; when the chosen name exists the build uses `name_2` and reports `name_adjusted`.
* 2026-11-23 (P10): the guide says the client is "an OpenAI-compatible chat client (`openai` SDK or plain `httpx`)". Neither is a declared dependency: `httpx` only arrives
  through `mcp`, and the pinned `mcp` 2.3.0 installs it as `httpx2`. The client therefore uses the standard library (`urllib.request`) behind a small `Transport` protocol, with no
  redirects (a redirect would forward the bearer token) and a response size cap. No dependency was added to `pyproject.toml`.
* 2026-11-23 (P10): the guide says "at least `min_cases` valid cases must remain" without saying whose. The threshold counts the challenger's own usable cases (after the invalid,
  schema-forbidden and duplicate ones are dropped), not the host's, because counting the host's cases would let a model that returns nothing pass. The host cases are always run and
  judged first, whatever the model returned. Fewer than `min_cases` is `failed` ("challenger produced too few usable cases"); an LLM error is `skipped`.
* 2026-11-23 (P10): the guide says a `high` confidence failure blocks and a `medium` one warns. A crash, timeout, oversized or non-JSON result on a schema-valid input blocks whatever the case's
  confidence or check, because the input was checked against the tool's own schema on the host and no expectation can make a crash acceptable. A `ToolError` is a handled refusal, as in G4.
* 2026-11-23 (P10): the guide lists three new settings. `challenger_max_rpm` falls back to `llm_max_rpm` only while the challenger has no endpoint of its own (a different endpoint is a
  different quota). Limiters are shared per process by role, endpoint and rate, so two verifications that run at once share one schedule. `llm_timeout_s` is bounded 1 to 600.
* 2026-11-23 (P10): scope additions the guide does not list: `LLMError`, `LLMUnavailable`, `LLMRefused` and `LLMBadReply` in `errors.py`; `llm/__init__.py`; the `toolforge.llm` package-data
  line in `pyproject.toml` for the prompt file; a `challenger model` check in `toolforge doctor` (the guide asks for the same-model warning there; the check also reports "not configured" and a
  bad base URL, and makes no request); `Settings.effective_challenger_max_rpm` and `.challenger_shares_builder_model`; a G5 section in `docs/verification.md`; and the tests `test_llm_client.py`,
  `test_checks_dsl.py`, `test_g5_challenger.py`, `test_g5_prompt.py` and `test_llm_live.py`. The harness was not changed: `--batch` with `include_results` already does what G5 needs.
* 2026-11-23 (P10): the model id goes to the ledger as a metric of the `forge.gate.*` event (`metrics.model`, token counts and case counts), not as a new event type, so the catalogue in
  `02-architecture.md` is unchanged. `tests/test_llm_live.py` reads `TOOLFORGE_LLM_*` when the file is collected, because `tests/conftest.py` removes them from the environment before every test.
* 2026-11-22 (P09): scope additions the guide does not list. `SandboxSpec.broker` (the service names a sandbox may call; a sandbox with brokered
  services is attached to the egress proxy even with `network="none"`, because the broker is a path on the proxy). `ToolRunner.run(record_stats=)`
  (the live smoke run must not count as a use). `ToolRunner(broker_services=)`. `report_view`/`forge_status` show the approval state. `Pipeline.hand_over`
  (a version that was verified but whose approval request could not be opened is handed over when the same package is submitted again, instead of
  staying stuck). The guide put `decide` in `approvals.py`; it lives in `operator_actions.py` (where `promote`, `release` and `quarantine` are, and which
  the server never imports), and `approvals.py` holds everything the server may safely call because it can only say no. G7 is a class in
  `GATE_CLASS_PATHS` but not in `PIPELINE_GATE_IDS`: it hands over to the operator and does not wait.
* 2026-11-22 (P09): the guide says broker configuration is `MINILOCKER_BROKER` and `MINILOCKER_BROKER_TOKEN_<NAME>`. Those are MiniLocker's variables,
  read from the process environment, not by `Settings`, so `.env` (which holds only `TOOLFORGE_` settings) does not reach them. `docs/approvals.md` says to
  export them. No new `Settings` field was added.
* 2026-11-22 (P09): MiniLocker's proxy has one allowlist and one broker for every sandbox on it, and lets a host's subdomains through. The guide's
  "approved hosts only" therefore holds for what the manifest declares and what the report measures, not for what the proxy enforces. Documented in
  `docs/approvals.md`, the Blast Radius footer and `docs/upstream/minilocker-per-sandbox-allowlist.md`; a real fix is upstream.
* 2026-11-22 (P09): `FOOTER` of the Blast Radius report was clipped to 300 characters by `clean()` in the stored and loaded document, so its last sentences
  never reached a reader. The footer is now a constant that is set after cleaning and never taken from a stored file.
* 2026-11-22 (P09): a dev run (`--dev-unverified`) of a package that declares network hosts or secrets is refused (it never gets either), instead of
  running with no network and failing inside the tool. `tests/docker/test_egress.py` was written but not run: no Docker daemon was available.
* 2026-11-22 (P08, found by `pytest -m docker`): MiniLocker's audit reports `rootfs_changes=1` for a tool that wrote nothing, because `docker diff` lists
  the tmpfs mount point `/workspace` in its diff. G6 and the Blast Radius report no longer count an entry whose path is exactly a mount point, of any
  kind (`sandbox.base.real_rootfs_changes`); any other path still counts. The first attempt ignored only "modified" entries and was wrong: the real kind
  is most likely "added" (Docker creates the mount point), so G6 now also names the kind of each change it reports. Until this was fixed G6 failed every tool on
  a real daemon. The report also treats a verification ledger with no G6 start as not measured instead of measuring the other gates' sandboxes.
* 2026-11-22 (P08): the guide says G6 checks "no undeclared egress". G6 runs with the network closed, so no connection can be attempted and egress
  is reported as `not applicable`, never `clean`. Egress against approved hosts is measured at run time in P09.
* 2026-11-22 (P08): `tests/test_import_boundary.py` allow-list gains `toolforge/report/blast_radius.py`, which loads MiniLocker's `ledger.report`
  by name through `_ml_path.load`. This is the extension the test's own docstring provides for P08.
* 2026-11-22 (P08): the Blast Radius report is stored at `ledgers/<tool>@<version>/blast-radius.json` and found by that path, not through a new
  registry column, so no migration is needed. It is a derived copy: `toolforge report` rebuilds it from the ledgers.
* 2026-11-22 (P08): a G6 that could not observe the run (no decoys planted, hardening unreadable, incomplete audit, nothing to run) is `skipped`, not
  `passed`, so the version stays `verified_basic`. With the local stand-in runner G6 is therefore always skipped.
* 2026-11-22 (P08): `FakeRunner` and `FakeHandle` take `events=` (a list of `(type, payload)`) so tests can script `canary.*` and `egress.*` events.
  `ledger.py` gains `read_ledger`, `verify_chain` and `version_ledger_dir` for the report; the report never needs Docker.
* 2026-11-22 (P08): G6 does not use the author's test literals as inputs (the spec allows it); it uses the non-live manifest examples and 10 generated inputs.

* 2026-11-21 (P07): the guide says the SDK class is `FastMCP`. In the pinned `mcp` 2.3.0 it is `mcp.server.mcpserver.MCPServer` (tools are added with
  `add_tool`, progress is `Context.report_progress`, the HTTP app is `streamable_http_app()`, the test client is `mcp.Client(server)`, which speaks
  the 2026-07-28 protocol by default and the 2025-11-25 one with `mode="legacy"`). `mcp` also needs `httpx2`, `starlette` and `uvicorn`, which
  `pip` installs with it. The code and the tests use the real names.
* 2026-11-21 (P07): `MetaTool` has the fields the guide names (`name, summary, inputs, returns, since`) and one more, `description`: the text a
  model reads for that tool. It lives next to the row so the table and the registration cannot drift apart. `build_server` refuses to start
  when `META_TOOLS` and the handlers differ.
* 2026-11-21 (P07): what the SDK already did for HTTP, and what was added. For a loopback host the SDK checks `Host` and `Origin` against exact
  values (`127.0.0.1:PORT`, `localhost:PORT`, `[::1]:PORT`) and answers 421 or 403; for any other host it checks nothing. It has no token check.
  ToolForge switches the SDK's check off (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) and puts `netguard.NetGuard` in front
  instead, so the rules are the same for every bind address, a configured name or proxy works
  (`TOOLFORGE_SERVER_ALLOWED_HOSTS`, `TOOLFORGE_SERVER_ALLOWED_ORIGINS`, two settings the guide does not list), the token is required beyond
  loopback, and the dashboard (P17) uses the same code. The SDK still checks the content type.
* 2026-11-21 (P07): the guide says to leave `expose_direct` off if the SDK cannot add and remove tools at runtime. It can, but not through its
  tool registry: `ForgeServer` overrides `list_tools` and `call_tool` (public methods) and reads the registry on every listing. The
  tools-list-changed notification is sent through the SDK's `InMemorySubscriptionBus` by a small watcher that polls the registry every two
  seconds, because `toolforge promote` runs in another process and cannot call into the server. Only clients on the 2026-07-28 protocol (the
  `subscriptions/listen` stream) receive it; the SDK has no such channel for the 2025-11-25 protocol, whose clients see a change the next time they
  list. Tools that take caller files and tools that are not `stateless` are not offered directly.
* 2026-11-21 (P07): the SDK formats an argument that fails its schema as a pydantic message that echoes the caller's own value (shortened) and
  names a pydantic URL. It contains no paths or stack, so it is passed through. Everything that raises inside a handler is caught first: a
  `ToolForgeError` becomes its own message, hint, `code` and a `ref`; anything else becomes "an internal error" with a `ref`, and the details
  go to the log under that `ref`. Failures of the machine (`SandboxUnavailable`, `SandboxError`, `LedgerError`, `ConfigError`) are never forwarded.
* 2026-11-21 (P07): `forge_find`'s `confidence` is the share of the query's words found in the tool's name, description and `when_to_use`
  (`high` from 75 %, `medium` from 40 %). bm25 scores from a small index cannot be compared between queries, so they are not used as thresholds.
  The response says so in `confidence_basis`; P12 replaces it.
* 2026-11-21 (P07): the guide says promote refuses `rejected`, `quarantined` and `active` versions. It refuses every status other than
  `verified_basic` and `verified`, with a message for each, and (from the P06 notes) a version without a stored blob or whose latest G2 row is
  not `passed`. It also needs a reason, and it writes `forge.promoted` and the `forge.deprecated` events of displaced versions to
  `ledgers/<tool>@<version>/operator.jsonl`.
* 2026-11-21 (P07): scope additions the guide does not list: `Busy` in `errors.py` (a `forge_submit` that waited 30 s for a verification slot);
  `queue_timeout_s` on `Forge`; a `progress` callback on `Pipeline.run` and `Forge.submit` (`ProgressFn`; it never changes a result and a callback
  that raises is ignored); `services/operator_actions.py` (the operator-only actions; the server never imports it), `server/direct.py`,
  `server/runtime_info.py`, `adapters/__init__.py` (reads the recipes), `cli_serve.py`, `cli_promote.py`, and the new-version rule from P12 (a
  new version of an active name must have `parent_version` equal to the active version, else `name_collision`) in `Forge`.
* 2026-11-21 (P07): the guide's `forge_run` takes `files: object | null`. It is an object that maps a file name to its text, or to
  `{"base64": "..."}`; the file lands at `inputs/<name>`. The Python version in `forge_runtime_info` is read from the runtime image like the library
  list (the guide's "Python version" with no source); it is `null` when the image cannot be read. The server's own Python is shown separately.
* 2026-11-21 (P07): not run where the patch was written: everything marked `docker`, `toolforge submit examples/toolpacks/csv_summary` through
  real containers, `forge_runtime_info` against a real image, and any real MCP client program (Claude Code, Claude Desktop, Cursor). The tests use
  the SDK's own client against the server in memory, over a real loopback HTTP port and over a real stdio subprocess.
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
* 2026-11-24 (P12): the guide's scope names `core/repair.py`; the code lives in `services/repair.py` (the guide's `core/` is `services/` here, as in P11). Scope additions,
  each needed by something the guide asks for: `registry/health.py` (storage behind three new tables, so `registry/__init__.py` stays a facade), `services/failures.py` (failure
  fingerprints, the raw-input rule and the quarantine verdict, called by the runner), `cli_health.py` (`toolforge rollback` and `toolforge problems`), and a third migration
  table, `repairs`, because the per-hour limit and the "flagged for the operator" rule need attempts that survive a restart. `regression_cases` holds one row per captured
  failure (a row is a replayable case only when it has an `input_json`).
* 2026-11-24 (P12): `forge_status` shows a version's problem reports to agents as a **count**, with the command the operator uses to read them, and never the text: a report is
  written by another agent and `forge_status` is read by agents, so showing it would hand one agent an instruction channel into another's context. `toolforge problems` shows the
  text, cleaned.
* 2026-11-24 (P12): the guide lets a report be rate limited "per caller", but an MCP connection carries no caller identity here, so every report has the reporter `agent` and the
  limit (5 an hour) is per server. It cannot be finer without identity that the transport does not provide.
* 2026-11-24 (P12): the Python call stub writes an enum as `Literal[...]`, so the test of allowed names admits `Literal` beside `Any`, `dict`, `list`, `str`, `int`, `float`,
  `bool` and `None`. A stub whose Python form is over 2000 characters is replaced by the plain `args` form alone: the TypeScript one is replaced only if it is over the limit too.
* 2026-11-24 (P12): the guide says a failure is `Crash`, `Timeout`, `SandboxCrashed` or `NoResult` for automatic quarantine; `OutputTooLarge` and `NotJson` still count against a
  tool's success rate (as in P07) but not toward quarantine. A handled `ToolError` is left out of the window altogether (neither failure nor good call), which the guide leaves
  open. `PolicyViolation` is captured but is handled by P08's immediate quarantine, not by the window.
* 2026-11-24 (P12): the name-collision rule "a higher version whose `parent_version` equals the active one" was already enforced for any valid manifest, because the manifest rule
  says a parent version must be lower than the version. P12 adds the check to `Forge._check_lineage` anyway (error `version_not_higher`) so it holds if that rule ever changes.
* 2026-11-24 (P12): `tests/test_context_budget.py` registers 10 and then 60 tools, not 200: building each verified tool takes about half a second and 200 made the test take 100
  seconds. The budgeted responses do not depend on the registry's size, which the test checks by comparing the two sizes. `BUDGET` values: `tools/list` 14000, `forge_find` 9000,
  `forge_describe` 20000 characters (measured 12.3k, 1.9k and 3.3k with the reference tools).
* 2026-11-24 (P12): `Builder` no longer needs `confidence=` from the server: unless a test injects one it uses the search hit's own `confidence`, so a build reuses a tool exactly
  when `forge_find` would call it `high` (including the lowering for a tool that keeps failing). `server.tools.confidence(query, record)` is kept for text-only callers.
* 2026-11-24 (P12): three existing tests changed because P12 replaced what they pinned: `tests/test_registry_db.py` (the table set; its stand-in migration no longer reuses the name
  `regression_cases`), and `tests/test_registry_search.py` (fallback scores are no longer bounded by 1, since the formula now adds terms). The exact tool list tests gained
  `forge_repair` and `forge_report_problem`.
* 2026-11-25 (P13): the code is in `src/toolforge/services/sessions.py` (`SessionManager`), not `core/sessions.py`: this repository has no `core/` package, and `services/` is where the runner and the builder live. The registry side is `registry/sessions.py`, migration 4 (`sessions` table).
* 2026-11-25 (P13): a session id is a capability, `s-<8 hex>.<32 hex>`. MCP carries no caller identity, so "a caller cannot use another caller's session id" can only mean that the id is unguessable: only the SHA-256 of the secret half is stored, an unknown id and a wrong secret give one identical error (`bad_session`), and lists show the public half only.
* 2026-11-25 (P13): ledgers are split because a sandbox writes its own ledger while it is open and a ledger has one writer. Each sandbox of a session has a box ledger (`<public>-<n>.jsonl`, which Blast Radius samples as calls), and the session's `forge.session.*` events go to `sessions/<public>.jsonl` (hashes and sizes only). The guide's "records a ledger event `forge.session.step`" is the second one.
* 2026-11-25 (P13): the real-secret audit runs when a box is closed (end of session, rewind, recovery), not after every call, because the audit destroys the sandbox. The decoy tripwire does run after every call, as P08 does. An answer delivered before a late audit finding was already delivered; such a finding (a decoy event the per-call check missed, or a real secret) quarantines the tool version and closes the session as `quarantined`.
* 2026-11-25 (P13): a failed call (including `ToolError`, a crash, a timeout, too-large or non-JSON state) is not a step and saves no state. A sandbox that died or timed out is replaced automatically by a new one restored from the last step; if that fails the session is closed as `lost`. The guide does not say.
* 2026-11-25 (P13): each snapshot is capped at 8 MiB (`MAX_SNAPSHOT_BYTES`); one that is bigger is not kept, so that step cannot be rewound to. The last 5 are kept in memory as the guide says.
* 2026-11-25 (P13): the package, harness and SDK are put back before every call (not "thawed once"), so a session tool cannot rewrite them for its next call. Its state and its other files persist.
* 2026-11-25 (P13): there is no static check that the entrypoint of a `kind: "session"` tool takes two parameters; a wrong signature shows up as a `Crash` in G3.
* 2026-11-25 (P13): `tests/test_context_budget.py` allows `tools/list` 15000 characters instead of 14000 (measured 14.7k with the four P13 tool entries; the descriptions were already trimmed), as `server/budget.py` requires a line here for any change to a budget.
* 2026-11-25 (P13): `max_sessions` is counted in the registry (open sessions of every server process on the data directory), not in memory, so two servers cannot together exceed it. Because a crashed server leaves rows behind, a session only counts while its owner's heartbeat is under 45 s old.
* 2026-11-25 (P13): `toolforge run` on a session tool runs one call and closes the session, and so does the operator's live smoke run during approval. Existing tests changed: `tests/test_tool_runner.py` (a session tool used to be refused, now it goes to the session manager), `tests/test_registry_db.py` (table set), `tests/test_harness.py` (`hashlib` is allowed in the harness), `tests/test_cli_doctor.py` (live-session containers are left alone), `tests/test_docs_clients.py` and `tests/test_server_tools.py` (the three new tools).
* 2026-11-26 (P14a): P14 was split (`AGENTS.md`): P14a is the runtime image, the recipes and the seven non-browser packs; P14b is browser plans and `docs/gui-kit.md`. `docs/guide/patches/P14-adapters.md` became `P14a-runtime-image-and-kits.md` and `P14b-browser-plans.md`, `scripts/check_guide.py` accepts a letter suffix, and a bare `P14` in a document means both parts. `browser-plan` is not in `RECIPE_KINDS` yet.
* 2026-11-26 (P14a): the default `runtime_image` changed from `python:3.12-slim` to the locally built `toolforge-runtime:py312`. A fresh checkout cannot run a tool until `toolforge images build` has run once; `toolforge doctor` and the sandbox error say so. Existing tests that pinned the old default were updated, and the docker tests that run tools skip when the image is missing.
* 2026-11-26 (P14a): the image's base is pinned by tag (`python:3.12-slim`), not by digest, and the libraries are pinned by exact version without hashes. The image id in the ledger (`image_id`, `null` when Docker could not be read) is what tells two builds apart. The versions were resolved in a Python 3.12 venv and have not been built inside the base image here.
* 2026-11-26 (P14a): a manifest example can only carry text, so `sqlite_query` cannot show a binary `.db` in an example; it accepts a `.sql` dump as well (loaded into memory with an authorizer that refuses `ATTACH` and every PRAGMA except `foreign_keys`) and its tests build a real `.db`. `http_json_fetch` has one `live` example, which carries an `output` (a live example is not compared) so that the manifest does not warn that no example checks behaviour.
* 2026-11-26 (P14a): a tool's input schema must not rest on `pattern` alone: G4 builds its inputs from the schema and is `skipped` when it has nothing to try, which blocks approval. `http_json_fetch` validates its path in code instead and the `http-api` recipe says so.
* 2026-11-26 (P14a): `forge_recipe` serves five recipes through the existing fencing and 20000-character cap; the `forge_recipe` entry of `META_TOOLS` builds its list of kinds from `RECIPE_KINDS`. The recipes' packs use only the standard library so their test runs anywhere; the reference packs use the image's libraries (`chess`, `gymnasium`) and their tests skip where those are missing.
* 2026-11-26 (P14a): the spec's "delivering the files a tool writes with `output_path`" is not done in P14a; it is not named in this patch's scope and nothing in the packs needs it.
* 2026-11-26 (Fix P14a): `python-chess` is pinned to 1.10.0, not 1.11.2: the 1.11 releases publish no wheel, and the runtime image installs with `--only-binary=:all:` so that nothing is compiled in the build. `docs/runtime-image.md` says that every pin needs a wheel and gives the `pip download` command that checks it.
