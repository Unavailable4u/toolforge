# P11: Server-side builder

Depends on: P07, P10.

## Goal

Let any client, including a weak or non-coding one, say what it needs in words and receive a verified tool. The server uses
its own LLM to write a package, runs it through the pipeline, and feeds failures back until it passes or gives up. The same
code powers the benchmark and the demo.

## Scope

`src/toolforge/core/builder.py`, `src/toolforge/llm/prompts/builder.md`, MCP tool `forge_build` (appended to `META_TOOLS`; the README tool table and `docs/clients.md` change in the same patch),
CLI `toolforge build "<goal>"`, tests with `FakeLLM`. The `package-format` recipe and `forge_runtime_info` already exist (P07);
this patch only reads them.

New settings (each with a documented default and an `.env.example` line, so the drift test stays green): `builder_max_rounds`
(4) and `builder_token_budget` (60000).

## Tool: `forge_build`

Input: `goal: str` (what capability is needed, plain language), `examples: [{input, output?}] | null`,
`sample_files: {path: text} | null` (small samples of the data the tool must handle, at most 20 KB), `constraints: str | null`.
Output: the final `VerificationReport`, the number of rounds used, and either the usable tool name and version or a clear
failure summary.

## Algorithm

1. **Reuse first.** Search the registry with the goal. If a high-confidence active match exists, return it with
   `reused: true` and do not build.
2. **Draft.** Prompt includes the package format and harness limits (the `package-format` recipe from P07), the sandbox facts that
   matter (no network unless declared, preinstalled libraries from `forge_runtime_info`, small workspace), the goal, examples
   and sample files. The model returns a package as JSON. Sample files pass `secretscan` before they are sent (P10 client).
3. **Verify.** Run `Forge.submit`. On failure send back only the failed gate id, its summary and hints (no raw logs), plus
   the previous package, and ask for a corrected full package with a bumped patch version.
4. **Stop** on success, after `max_rounds` (default 4), when two consecutive rounds fail the same gate with the same summary,
   or when the token budget (`TOOLFORGE_BUILDER_TOKEN_BUDGET`, default 60000) is exhausted. A rate-limit or provider outage
   (`LLMUnavailable`) also stops the build with a clear message; it never loops.
5. Return everything the caller needs to decide what to do next. A failed build leaves its rejected versions in the
   registry (history is evidence), flagged as builder output.

A successful build ends at whatever the pipeline decides. With no challenger configured that is `verified_basic`, and the
operator promotes it (`toolforge promote`, P07); the build report says so in `next_action`.

Rules: the builder never requests network or secrets unless the goal explicitly requires an external service, and then
says which host and why in the description; it never copies sample data verbatim into code beyond what tests need; versions
are bumped by the loop, not the model.

## Concurrency and cost

Builds share the verification semaphore. Record per build: rounds, tokens, wall time, final status. Expose them in the
returned report and the ledger (`forge.build.finished`). Free API tiers rate-limit; the client's limiter and backoff (P10) apply.

## Tests

Scripted `FakeLLM`: first draft fails G3, second passes; stuck loop detection; budget stop; reuse short-circuit; the model
returning prose instead of JSON; the model asking for network without justification (the prompt forbids it; the gate set
catches it at G1/G7 anyway); provider outage stops the build without a loop; a build with no challenger ends `verified_basic`
and says how to promote. `llm` marked test builds a trivial tool end to end against a real endpoint.

## Acceptance

`pytest -q`; with an endpoint and Docker: `toolforge build "count words in a text"` ends with a verified tool (`active` once G5 is
configured or after `toolforge promote`).

## Pitfalls

The builder LLM is another untrusted author. Its output goes through exactly the same pipeline as an agent's submission,
with no shortcuts and no elevated status.
