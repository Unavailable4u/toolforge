# P16: Benchmark harness

Depends on: P11, P12, P14a, P14b.

## Goal

Replace claims with numbers. Measure whether agents finish tasks that need a tool they do not have, with and without ToolForge,
and whether reuse makes later tasks cheaper. Report intervals, not anecdotes.

## Scope

`bench/{tasks/,agent.py,harness.py,stats.py,report.py,context.py,synthetic.py,__main__.py}`, `tests/test_bench_*.py`, `docs/BENCHMARK.md`.

## Configurations

* `baseline`: the agent has one tool, `run_python(code)`, executed in the **same** hardened sandbox via the same runner, no
  registry, no verification, no reuse. This isolates what ToolForge adds beyond "can run code safely".
* `forge_cold`: the agent has the ToolForge MCP tools and an empty registry.
* `forge_warm`: the same, with a registry pre-seeded by running the sibling tasks first (the variants below).

The agent is a small, fixed tool-calling loop over any OpenAI-compatible endpoint (`bench/agent.py`), connected to ToolForge
through the official MCP client (stdio), with the same system prompt, step budget (default 20 tool calls), token budget and
model in every configuration. Record the model id and prompt hash in every result file.

## Tasks (`bench/tasks/`)

Each task defines: id, tier, the prompt, seeded input generation (`seed` makes data reproducible), a **hidden checker** that
inspects the final answer and, where relevant, produced files, and an optional "sibling" variant with different data for reuse
experiments. Minimum set (extend freely):

1. `log_stats`: parse an unusual log format given in a spec and compute statistics. (tier 1)
2. `sqlite_join`: answer a multi-table question over a SQLite file. (tier 1)
3. `tlv_convert`: convert a binary TLV file to JSON using a written spec. (tier 1)
4. `fixed_width`: parse a fixed-width report with quirks. (tier 1)
5. `messy_dates`: normalise ambiguous date strings with stated rules. (tier 1)
6. `yaml_diff`: semantic diff of two YAML files per a written rule set. (tier 1)
7. `blackjack_basic`: play N hands with a basic-strategy table and report the win rate within a tolerance band. (tier 3)
8. `frozenlake_path`: find a safe path with a search algorithm using a game tool. (tier 3)
9. `chess_mate_in_2`: solve three seeded mate-in-two puzzles through a chess tool. (tier 3)
10. `http_mock_fetch`: call a local recorded API through an approved host (mock server fixture). (tier 1)
11. `browser_fixture_headlines`: extract fields from a fixture web page served locally. (tier 2)
12. `hostile_data_file`: a data file containing prompt-injection text and a credential lookalike; the task must still succeed
    and nothing leaks. (safety)

Siblings for tasks 1 to 6 (different generated data, same capability) drive the reuse experiment.

## Metrics

Per configuration and task, over `--runs` (default 5, resample seeds): success rate with a **Wilson 95 percent interval**;
median tool calls and tokens; time; for forge runs, tool reuse rate, builds attempted, builds rejected per gate, and safety
counts (policy violations, decoy events). Aggregate per tier and overall. Add the Attack Lab containment rate as a separate
line.

## Context cost (no LLM, no Docker)

The idea behind `forge_find` is that a model pays for the tools it uses and not for every tool that exists. Measure it instead of
assuming it. `python -m bench context --sizes 10,100,1000 --seed 1` builds a seeded **synthetic** registry of that many active tools
(`bench/synthetic.py`: tools generated from a few templates with typical manifest sizes, registered with scripted gates; `bench` may not
import from `tests/`, so the helpers are small and local) and, using the real server in memory through the official MCP client, records
for each size:

* `meta`: the `tools/list` payload of the default server, one `forge_find` at the default limit, and one `forge_describe` of the top hit
  (the realistic "find, read, use" path);
* `direct`: the `tools/list` payload with `TOOLFORGE_EXPOSE_DIRECT=true`, which is what a server that lists every tool costs;
* `chain` (only if S02 is built): the request and response of a three-step `forge_chain` against the same three steps as separate `forge_run` calls.

Each figure is bytes and tokens. Tokens are `server/budget.py:estimate_tokens` (an estimate, labelled `"method": "estimate"`) unless
`--endpoint` is given, in which case the payload is sent as tool definitions in one request with `max_tokens` 1, once with and once
without, and the difference in `usage.prompt_tokens` is recorded with `"method": "measured"`. `summary.json` gets a `context` block
(`null` when not run, never zeros). The report says what the numbers are: synthetic tools of the stated sizes, one model's tokenizer or
an estimate, and **nothing about any other system**; do not put another project's numbers next to ours.

Hypotheses to write in `docs/BENCHMARK.md` before running: the `meta` payload for `tools/list` does not grow with registry size and
`forge_find` stays inside its budget; `direct` grows roughly in proportion to the number of tools. Report whichever way it falls.

## Report

`python -m bench --config all --runs 5 --out runs/bench/<timestamp>/` writes raw JSON per run (with ledgers),
`summary.json` (machine-readable aggregates: per configuration and task the success rate with its Wilson interval, run count,
medians, plus model id, prompt hash and date; the website and the dashboard read this file, and a missing measurement is `null`,
never zero) and `report.md` with tables and a short, plain "what this shows and does not show" section. State hypotheses beforehand in
`docs/BENCHMARK.md` (for example: forge improves first-attempt success on unfamiliar formats; warm beats cold on cost; safety
metrics are unaffected) and report results whichever way they fall. Small samples get wide intervals: show them.

## Tests (no Docker, no LLM)

Context cost: `estimate_tokens` on known strings; the same seed gives byte-identical synthetic registries and measurements; `meta`
`tools/list` is the same for 10 and 1000 tools; `direct` grows with size; a missing measurement is `null` in `summary.json`; the report
builder labels `estimate` and `measured` correctly (with a fake endpoint that returns `usage`). Wilson interval against known values; checkers accept a correct answer and reject several wrong ones for every task; seeded
generators are deterministic; the report builder with synthetic results; the agent loop with a scripted fake LLM and a fake
MCP backend.

## Acceptance

`pytest -q`; with Docker and an endpoint `python -m bench --config baseline --tasks log_stats --runs 2` completes and writes a report.

## Pitfalls

Never let a checker see the agent's chain of thought; never give the agent a hint the baseline lacks; never drop failed runs
from the tables; never describe a difference as significant unless the intervals support it.
