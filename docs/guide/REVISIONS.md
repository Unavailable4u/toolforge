# Revisions

How the guide itself changed, newest first. A patch author uses this to tell a deliberate refinement from an accident.

## Revision 4 (2026-11-23)

Written after P09 was built, in response to the Code Mode and programmatic tool calling discussion. Guide only; no code, no change to the
language, the gate list, the seam or the order of P10 onward. The post's numbers were not verified and nothing here relies on them.

### Added

| # | Idea | Where it lives | Why there |
| --- | --- | --- | --- |
| 1 | Typed signatures: `forge_describe` returns a Python and a TypeScript stub built only from the schema, with strict handling of hostile property names, enum values and size. | P12 (`server/signature.py`) | P12 already reshapes find and describe. A signature is a code position a model reads, so it must carry no author text. |
| 2 | A response size budget with a test that `tools/list` and `forge_find` do not grow with the registry. | P12 (`server/budget.py`) | The point of `forge_find` is that context cost stays flat; a test keeps it so. |
| 3 | Context cost per response, with estimate versus measured labelled, from 10 to 1000 synthetic tools. | P16 (`bench context`), P19 (site) | It checks the context benefit instead of assuming it. No other system's numbers are quoted. |
| 4 | Composition as a data-only chain of verified, network-free, secret-free tools, one sandbox per step, one fence per response. | S02 (optional) | Composition widens reach, so it comes after approvals (P09) and containment (P08), and the narrow version is the only one specified. |

### Checked and not changed

* A model-written script run by the server, and a tool that calls other tools from inside a sandbox, were considered and left out of S02. Both create
  a code path or a channel the threat model does not cover.
* P10 and P11 are untouched.

## Revision 3 (2026-11-19)

Reviewed the whole starter once more against its own rules, with `scripts/check_guide.py` and a line-by-line read of P12 and P13.
Still no code: only the guide changed. Nothing here alters the language, the gate list, the seam or the patch-by-patch workflow.

### Gaps found and fixed

| # | Gap in revision 2 | Fix | Where |
| --- | --- | --- | --- |
| 18 | With no LLM configured every version stops at `verified_basic`, `promote` refuses network or secrets, and approval only accepted versions that had passed G1 to G6. A tool that needs network could therefore never become active on the no-key path. | `verified_basic` may go to `pending_approval`; the request names the skipped gates. `promote` still refuses it. | P03, 02, P07, P09 |
| 19 | Rollback needs a `deprecated -> active` transition that did not exist, and its target was "a previous verified version", which excluded versions the operator had promoted by hand. | New transition; the target is any version that was active before (it already passed every gate and approval). | P03, 02, P12 |
| 20 | P08 introduced quarantine but the way back (`release`) arrived in P12, so a tripwire quarantine in M3 had no exit. | `release` and manual `quarantine` CLI commands move to P08. | P08, P12, P17 |
| 21 | P17 offered rollback, release and sessions but depended only on P09. | P17 now depends on P09, P12 and P13; the milestones page says what changes if P12 or P13 is dropped. | P17, PROGRESS, 05 |
| 22 | In P12 a problem report counted toward quarantine, so any confused or hostile caller could pull a tool for everyone. The site copy already said a report never pulls a tool. Handled `ToolError`s were not excluded from the failure rate. | Reports are shown to the operator only. Quarantine uses measured crashes, timeouts and `PolicyViolation`, not caller mistakes. | P12 |
| 23 | P12 and P13 added MCP tools and settings without the `META_TOOLS` and `.env.example` rules; P11's builder limits were not settings. | Each patch updates `META_TOOLS`, the README table, `docs/clients.md` and `.env.example`; named settings added. | P11, P12, P13 |
| 24 | P12 said the old version becomes `deprecated` when a repair succeeds, ignoring approvals and `verified_basic`. | The old version stays active until the new one is promoted; the lifecycle deprecates it. | P12 |
| 25 | The server-must-not-import-promote test would have needed re-inventing for `release`, `decide` and `rollback`. | One `OPERATOR_ONLY` constant in the test, extended by each patch. Session tools are documented as caller-scoped. | P07, P08 |
| 26 | The site's release build failed until someone hand-edited the repository address. | The address resolves from `--repo-url`, `site.json`, `GITHUB_REPOSITORY` or the git `origin` remote. | P19, PROGRESS |
| 27 | Three patch files lacked a required section (P04 Tests, P14 Scope, P21 Tests) and four titles disagreed with `PROGRESS.md`. | Fixed, and `scripts/check_guide.py` now guards all of it; P01 runs it as a test. | P04, P05, P06, P08, P14, P21, S01, P01 |
| 28 | P14 is too large for one pass and the guide did not say where to cut. | A suggested split into P14a and P14b. | P14 |
| 29 | The author works in WSL2 and the setup was one sentence. | `06-wsl2-setup.md`: Python 3.12 and venv on Ubuntu 24.04, Docker options, git settings, applying patches downloaded in Windows. | 06, README |

### Corrected while building P01

* The guide told readers to run `git am --check`, which is not a git option. The dry run is `git apply --check <patch>`
  (`AGENTS.md`, `06-wsl2-setup.md`).
* The current ruff formats code blocks inside Markdown, so `pyproject.toml` excludes `*.md` from ruff.

### Added

* `scripts/check_guide.py` (standard library only): patches versus table, dependencies, titles, required sections, index, patch
  references, stale wording, line endings.
* `docs/guide/06-wsl2-setup.md`. Site asset and checker notes in `NOTICE.md`.

### Removed

* `docs/guide/HANDOFF.md`, whose open items are the gaps above.

### Checked on 2026-11-19

* Every patch in `PROGRESS.md` has a file; every "Depends on" line matches the table; no dependency cycle; rows list dependencies
  first; every file named in the index exists; every `Pnn` mentioned is defined; no CRLF.

### Still not verified (unchanged)

* MiniLocker's pinned commit and each function signature (P04 reads the source first), the `mcp` SDK's current API (P07), exact
  dependency versions (P01), provider model names and limits. Nothing in the guide has been run, because nothing has been built.

## Revision 2 (2026-11-19)

The whole plan was read end to end before the first patch. The gaps below were fixed and the project website was added.
Nothing here changes the language (Python), the patch-by-patch workflow, the seven gates or the MiniLocker seam.

### Gaps found and fixed

| # | Gap in revision 1 | Fix | Where |
| --- | --- | --- | --- |
| 1 | A tool could not become `active` before P09/P10 existed. `forge_run` runs only `active` versions, and the operator action that moves `verified_basic` to `active` was never built. With no LLM key configured, everything stops at `verified_basic`, so this was also the normal no-key path. | `toolforge promote <name>@<version> --reason "..."` (operator only, CLI) | P07, button in P17 |
| 2 | P11 (builder) used the `package-format` recipe and `forge_runtime_info`, which P14 introduced three patches later. | Minimal `forge_recipe` and `forge_runtime_info` move into P07; P14 extends them. P11 now also depends on P07, because it adds an MCP tool. | P07, P11, P14 |
| 3 | The MCP HTTP transport could accept code submissions with no access control. | Loopback by default, bearer token required for any other host, `Origin`/`Host` checks, new settings. | P01, P07, P18 |
| 4 | The dashboard could start Attack Lab runs from a web page. | Removed. Attack Lab and Benchmark tabs are read-only views of stored result files. Runs stay on the CLI. | P17 |
| 5 | A builder model can paste a real key into tool source, and the registry would then store it. | G1 secret-pattern scan (includes Google API key shapes, since Gemini is a likely builder). | P06 |
| 6 | Builder and challenger models could share blind spots; free API tiers rate-limit; prompts leave the machine. | Recommended model diversity, client-side rate limit and backoff on 429 with `Retry-After`, a data-handling note. | P10, `04-optional-resources.md` |
| 7 | No CLI to check or scaffold a package. | `toolforge validate`, `toolforge new`. | P02 |
| 8 | Windows line endings break `git am`, deterministic tars and shell scripts. | `.gitattributes`, `.editorconfig`, a repository hygiene test. | starter, P01 |
| 9 | The event catalogue lacked events later patches emit. | Added `forge.build.finished` and `forge.session.*`. | `02-architecture.md` |
| 10 | README, docs and site would each hand-copy the tool list and gate list. | One source of truth each: `META_TOOLS` (P07), `GATE_INFO` (P06). Docs, drift tests and the site generator read them. | P06, P07, P19 |
| 11 | Naming for fix patches and stretch patches was ambiguous. | Defined in `AGENTS.md`. | `AGENTS.md` |
| 12 | No rule for a patch too big for one pass (P06, P14). | Split rule (`Pxxa`, `Pxxb`). | `AGENTS.md` |
| 13 | Blobs of rejected versions accumulate forever. | `toolforge registry gc` (blobs only; rows are never deleted). | P21 |
| 14 | The demo's recorded run was not kept in the repo, so the site could not show real data without Docker. | P18 commits one small, sanitised recording. | P18 |
| 15 | The benchmark wrote only a Markdown report, which a site cannot read reliably. | It also writes `summary.json`. | P16 |
| 16 | The MCP port and the dashboard would each need their own access checks. | One shared `netguard` module (loopback test, bearer check, `Origin`/`Host` allow-list). | P07, P17 |
| 17 | The Attack Lab did not cover the new surfaces. | Added `secret_in_source`, `http_no_token`, and widened `approval_over_mcp` to cover promote. | P15 |

### Added

* **P19 Landing site** and **P20 Guided tour**. The old P19 (hardening and release) is now **P21**.
* **S01** (optional stretch): portable bundles and signed provenance attestations.
* `docs/guide/04-optional-resources.md`: how the optional resources from the event page map onto this project.
* `docs/guide/05-milestones.md`: milestones, rough sizes and honest cut lines.
* `docs/guide/site-brief.md`: audience, plain-language rules, page map and design direction for the site.

### Checked and not changed

* Python stays. MiniLocker is Python and is imported in process, forged tools are Python, the MCP SDK and pydantic are
  Python, the work is I/O bound, and the security boundary is the container, not the language.
* The gate list, the lifecycle, the trust boundaries and the rule that only the operator approves.

### Facts checked on 2026-11-19

* MiniLocker (`github.com/shaikh-dotcom/MiniLocker`) exists, is MIT licensed, and has `backend/minilocker/` with `agent/`, `policy/`,
  `sandbox/`, `egress/`, `ledger/` and `api/` as the guide says. It has its own "Attack Lab"; ToolForge's is a separate suite
  aimed at hostile tool authors.
* Gemini's API documents an OpenAI-compatible endpoint at `https://generativelanguage.googleapis.com/v1beta/openai/`.

### Not verified (do this at the start of the patch that needs it)

* That the pinned MiniLocker commit is `0e76295a986292cdb07795d6efc1570f9d0c1acb` and that every function signature in
  `03-minilocker-integration.md` matches it. P04 must read the source first.
* The `mcp` SDK's current API (P07), exact pinned dependency versions (P01), and any provider's model names or free-tier limits.

## Revision 1

The original starter: guide, conventions, architecture, MiniLocker integration notes and patches P01 to P19.
