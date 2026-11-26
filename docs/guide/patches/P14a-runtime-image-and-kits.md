# P14a: Runtime image, recipes and reference packs (software use)

Depends on: P13.

## Goal

Give agents what they need to forge tools that **use other software**: query databases, convert file formats, call HTTP APIs and
play state-based games. Ship a curated runtime image, a set of recipes an agent can fetch, and tested reference packs. Be honest
about tiers. This is the first part of the split of the original P14 (see `AGENTS.md`); browser plans and the GUI kit are P14b.

## Scope

`images/runtime/Dockerfile` and `requirements.txt`, CLI `toolforge images build`, `src/toolforge/adapters/recipes/*.md` (`sql`,
`file-format`, `http-api`, `game-state`) and the `RECIPE_KINDS` registry constant, the `forge_runtime_info` extension (image id), the
image id in the ledgers, `examples/toolpacks/` (`sqlite_query`, `fixed_width_parse`, `tlv_decode`, `http_json_fetch`, `chess_session`,
`blackjack_session`, `frozenlake_session`), `docs/runtime-image.md`, and the tests below.

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
* `browser-plan` is added by P14b, which appends it to the registry constant.

`forge_recipe` returns the recipe text through the same fencing and size cap as other server text, marked as
operator-authored documentation.

## Reference packs and the golden test

`examples/toolpacks/`: `sqlite_query`, `fixed_width_parse`, `tlv_decode` (a made-up binary format with a written spec),
`http_json_fetch` (mocked tests, one live example), `chess_session`, `blackjack_session`, `frozenlake_session`. Each has real tests.
`tests/test_reference_packs.py` runs gates G1, G3 and G4 on each against the real harness through the stand-in sandbox, and
`tests/docker/test_golden_packs.py` submits every pack through the pipeline in the real image and asserts the final status
(G5 skipped when no endpoint). A golden pack that stops passing is a regression in the pipeline, not the pack.

## Tests

Recipe files exist for every kind in `RECIPE_KINDS` and only for those; every recipe's complete example package (the fenced `manifest`,
`tool.py` and `tests/` blocks) is built and run through the real gates (a drift test); the size cap; `forge_runtime_info` matches the
image's `pip list` in a docker test; the pins, the Dockerfile and `docs/runtime-image.md` agree; `toolforge images build` with a stand-in
for Docker; the image id in the ledger events.

## Acceptance

`toolforge images build`, `pytest -m docker tests/docker/test_runtime_image.py tests/docker/test_golden_packs.py -q`, `pytest -q`.

## Pitfalls


Games must be deterministic given a seed or the replay pattern breaks. The SQL kit must not allow writes or `ATTACH` of paths
outside `inputs/`. Browser plans never carry credentials; sites that need login are out of scope in v1.
