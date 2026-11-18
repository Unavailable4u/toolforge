# P14: Runtime image and adapter kits (software use)

Depends on: P13.

## Goal

Give agents what they need to forge tools that **use other software**: query databases, convert file formats, call HTTP APIs,
operate web pages, and play state-based games. Ship a curated runtime image, a set of recipes an agent can fetch, and tested
reference packs. Be honest about tiers.

## Scope

`images/runtime/Dockerfile`, CLI `toolforge images build`, `src/toolforge/adapters/recipes/*.md` and the registry constant that lists
every recipe kind, `src/toolforge/adapters/browser_plan.py` (the plan vocabulary and its validator), `sandbox/browser_adapter.py`
(allowed MiniLocker importer), the `forge_runtime_info` extension, `examples/toolpacks/` (eight reference packs),
`tests/docker/test_golden_packs.py`, recipe and plan tests, and `docs/gui-kit.md` (documentation only).

This patch is large. If one pass cannot meet the quality bar, split it as `AGENTS.md` describes: **P14a** the runtime image, the
recipes, `forge_runtime_info` and the tier 1 packs (`sqlite_query`, `fixed_width_parse`, `tlv_decode`, `http_json_fetch`) and the
three session packs; **P14b** browser plans (`browser_headlines`) and `docs/gui-kit.md`.

## Tiers

| Tier | Capability | How | Status in v1 |
| --- | --- | --- | --- |
| 1 | data and file software: SQLite/DuckDB queries, CSV/JSON/YAML/binary format conversion, text and image libraries | Python tools in the runtime image, caller files under `inputs/` | required |
| 1 | HTTP APIs | tools with declared hosts, mocked tests, live smoke after approval, brokered tokens | required |
| 2 | web pages | declarative browser plans executed by MiniLocker's browser sandbox (below) | required, smaller scope |
| 3 | state-based games and simulations | session tools over engines with a text/JSON state (python-chess, gymnasium toy-text) | required |
| 4 | desktop GUIs | virtual display image, screenshots and input injection | optional, experimental, documented only |

No tier promises that the agent can drive arbitrary software. Pixel-based control needs a vision step that MiniLocker does
not have; tier 4 stays optional for that reason.

## Runtime image

`images/runtime/Dockerfile` based on `python:3.12-slim` with pinned wheels only: `duckdb`, `pyyaml`, `python-dateutil`,
`beautifulsoup4`, `lxml`, `pillow`, `numpy`, `gymnasium` (no heavy extras), `chess`, `jsonschema`, `tabulate`, and the standard
library. Build with `toolforge images build` (a thin wrapper over `docker build`), tag `toolforge-runtime:py312`, make it the
default `runtime_image`, record the image digest in every ledger. `forge_runtime_info` (introduced in P07) now reports this image's
installed libraries with versions as well as the Python version and sandbox limits, so authors know what exists. Libraries baked
into the image do not count against the 64 MB workspace; extras installed at G2 do.

## Recipes (`src/toolforge/adapters/recipes/*.md`, fetched with `forge_recipe(kind)`)

Short, practical, with a complete minimal example each (manifest, tool, tests). `forge_recipe` and the `package-format` recipe
already exist since P07; this patch adds the others and the registry constant that lists every kind:

* `package-format` (from P07): the package, the harness constraints, the mini test runner (no fixtures), `ToolError`, size limits.
* `sql`: query a database file from `inputs/` with `sqlite3`/DuckDB opened read-only, `SELECT` only, row cap, parameterised
  queries, returning rows plus column names.
* `file-format`: parse or convert an unfamiliar format from a spec given in the task; test with a handcrafted fixture.
* `http-api`: declare hosts, structure the HTTP layer so tests can inject a recorded response, use a brokered secret by
  name, never read tokens from the environment.
* `game-state`: session tool pattern with `state` (seed plus action list replay), legal-move listing, observation as text,
  win/loss detection, a deterministic reset.
* `browser-plan`: the declarative plan format (below).

`forge_recipe` returns the recipe text through the same fencing and size cap as other server text, marked as
operator-authored documentation.

## Browser plans (kind `browser_plan`)

A forged web tool is **data, not code**: a JSON plan the host executes against MiniLocker's `BrowserSandbox`
(`sandbox/browser_adapter.py`, a new allowed importer; read `browser_worker.py` for the supported actions first). The manifest
has `kind: "browser_plan"` and `permissions.browser: true` plus the hosts it may open; the plan is a list of steps from a closed
vocabulary (`goto`, `click`, `type`, `extract_text`, `extract_attr`, `wait_for`, `screenshot`), with `{{arg}}` templating from
the tool input and `extract_*` steps naming output fields. The host validates the plan against the DSL, checks every
`goto` host against the approved list, runs it, and returns the extracted fields through the standard fencing (page text is
untrusted; use `textsafety` exactly as MiniLocker does). Verification for this kind: plan validity (G1), a dry run against a
fixture page served inside the test suite (G3), host and step limits (G4), the browser sandbox audit (G6), approval for hosts (G7).
Keep the vocabulary small; reject anything not in it.

## Reference packs and the golden test

`examples/toolpacks/`: `sqlite_query`, `fixed_width_parse`, `tlv_decode` (a made-up binary format with a written spec),
`http_json_fetch` (mocked tests, live smoke marked), `chess_session`, `blackjack_session`, `frozenlake_session`,
`browser_headlines` (plan). Each has real tests. `tests/docker/test_golden_packs.py` submits every pack through the whole
pipeline and asserts the expected final status (with the LLM gate skipped or faked when no endpoint). A golden pack that stops
passing is a regression in the pipeline, not the pack.

## Tier 4 (optional, experimental)

Write `docs/gui-kit.md` describing an `toolforge-runtime-gui` image (virtual display, screenshot and input tools), why it
needs a vision step, what could be built without one (apps exposing text state), and its risks. Implement only if the rest is
solid. If implemented: separate image, separate recipe, clearly labelled experimental, and no claim in the README beyond the doc.

## Tests

Recipe files exist for every kind in a registry constant; every code block in recipes that claims to be a runnable example is
extracted and validated by P02 validation (a drift test); plan DSL validator unit tests (unknown step, host not approved,
template injection attempts); `forge_runtime_info` matches the image's `pip list` in a docker test.

## Acceptance

`toolforge images build`, `pytest -m docker tests/docker/test_golden_packs.py -q`, `pytest -q`.

## Pitfalls

Games must be deterministic given a seed or the replay pattern breaks. The SQL kit must not allow writes or `ATTACH` of paths
outside `inputs/`. Browser plans never carry credentials; sites that need login are out of scope in v1.
