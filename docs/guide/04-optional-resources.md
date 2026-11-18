# Optional resources

Nothing in this project depends on any of these. They are listed because they fit, and each entry says where.
Model names, free-tier limits and credit amounts change often: copy them from the provider's page on the day you use
them, and never hard-code one in the repository.

## What the project needs, and what it does not

* **No API key is needed to start.** Everything except gate G5 (challenger), the builder (P11) and the benchmark agent
  (P16) works without any model. With no model configured, a submitted tool stops at `verified_basic` and you promote
  it yourself with `toolforge promote` (P07). That is the honest, fail-closed behaviour.
* **A model is needed for** G5, `forge_build`, `forge_repair` without a supplied package, the benchmark, and the live demo.
  The recorded demo (P18) replays without one.

## Mapping the event resources onto the project

| Resource (from the event page) | Use here | Required? |
| --- | --- | --- |
| Google AI Studio | Create an API key for Gemini. Used as the builder, challenger or benchmark-agent model. | no |
| Gemini API docs | Reference for the OpenAI-compatible endpoint (below) and for rate limits. | no |
| Gemma | Google's open-weights family. Candidate for a locally hosted challenger behind any OpenAI-compatible server, so prompts never leave your machine. Check the licence terms. | no |
| "Build tools for AI agents" (MCP tutorial) | Background reading for P07. The official SDK source is still the authority (see P07). | no |
| Native.Builder (optional development tool, per the page) | Could be used to sketch the landing page visuals. The repository's site is plain files built by `scripts/build_site.py`, so nothing depends on it. | no |
| LabLab tutorial library, Discord and live Q&A | Community help. Not part of the build. | no |

## Using Gemini through the OpenAI-compatible endpoint

The LLM client (P10) speaks the OpenAI chat-completions protocol, so Gemini works with three settings:

```
TOOLFORGE_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
TOOLFORGE_LLM_API_KEY=<key from Google AI Studio>      # .env only, never committed
TOOLFORGE_LLM_MODEL=<a Gemini model id copied from the docs>
```

Auth is a bearer token. Do not pass vendor-specific parameters by default; the client stays provider-neutral.

## Use two different models on purpose

The challenger (P10) exists to catch what the author missed. If the builder and the challenger are the same model, they tend
to miss the same things. Recommended setups, best first:

1. Builder and challenger from **different model families** (for example a Gemini model as builder and a local Gemma model
   as challenger).
2. Same family, **different sizes** or versions.
3. Same model for both. It works, but treat G5 as weaker evidence and say so in any results you publish.

Whichever you pick, record the model ids in the ledger (P10 already does) and in benchmark reports (P16 does).

## Free tiers and rate limits

Verification and building make several model calls per submission, and `max_concurrent_verifications` runs two at once.
A free tier can answer with HTTP 429. The client (P10) therefore limits its own request rate (`TOOLFORGE_LLM_MAX_RPM`),
backs off on 429 and honours `Retry-After`. A rate-limit failure makes G5 `skipped` (infrastructure), never `failed`, and
never passes. The builder token budget (P11) bounds total spend per build.

## Data handling (read this before using a hosted model)

Tool source, tests, manifests and sample files are sent to the model provider. Free tiers may allow the provider to use
prompts to improve its products; read the current terms. Therefore:

* Never send real credentials or private data. G1 scans for secret-shaped strings (P06), but that is a safety net.
* For private data, use a locally hosted model or run without G5 and promote by hand.
* The ledger records model ids and token counts, never prompts or code.

## Secrets hygiene

Keys live in `.env` (git-ignored) or the process environment. `toolforge config` masks them. The Attack Lab, the benchmark and
the demo must run with decoy or no credentials only. If a key ever lands in a commit, revoke it first, then clean history.
