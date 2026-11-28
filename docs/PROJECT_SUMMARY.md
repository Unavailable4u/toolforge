# ToolForge: project summary

**Title:** ToolForge

**One sentence:** An MCP server that lets an AI agent build, verify and reuse its own tools, safely: every tool is tested, attacked and
measured before it is published, and runs only inside a hardened sandbox.

## The problem

An agent that meets a task it has no tool for either gives up or writes code and runs it with the same trust as its own. Code written by a
model, possibly steered by a hostile web page or a poisoned file, should not run with the authority of the person who asked.

## The solution

When the agent has no tool for the job it asks ToolForge. The tool is written, put through seven gates, and published into a registry that any
MCP client can search. An operator, never the agent, decides what becomes usable. Tools run in a fresh sandbox with exactly the permissions
their manifest declares, and every step is written to a tamper-evident ledger.

## How it works

* **Forge:** the agent submits a package (code, tests, a manifest of what it may touch) or asks the server to build one from a goal.
* **Verify:** static checks, a build, self-tests that must notice a broken tool, schema fuzzing, an independent model's attack, a run with decoy credentials, and human approval for network or secrets.
* **Run:** a hardened Docker container per call, no network unless approved, output size-capped and fenced as data.
* **Reuse and repair:** tools are versioned, ranked by real outcomes, repaired through the same gates and rolled back.
* **Prove:** a hash-chained ledger and a Blast Radius report say what a tool did, and what was not measured.

## Intended users and use cases

Developers and researchers who give agents real work: data wrangling over unfamiliar files, database queries, simulations and games that keep
state, small browser tasks.

## Integrations

Any MCP client (stdio, or HTTP with a bearer token); MiniLocker for sandboxes, egress control and the ledger; Docker; any OpenAI-compatible
endpoint for the optional builder and challenger models.

## Technology

Python 3.12, MCP, FastAPI, SQLite, Docker, pydantic.

## Links

* Demo: `docs/DEMO_SCRIPT.md`, `demo/run_demo.py`, and one recorded run in `demo/recordings/main/`.
* Repository: this one; start with `README.md`.
* Reports: `docs/BENCHMARK.md` and `docs/ATTACKS.md` say how to measure; results exist only after you run them. Limits: `docs/KNOWN_LIMITATIONS.md`.
