---
marp: true
theme: default
paginate: true
---
# ToolForge

Agents that build their own tools, and a registry that makes them safe to reuse.

An MCP server. Sandboxed by MiniLocker. Every step on a tamper-evident ledger.

---

## The problem

- An agent meets a task it has no tool for, and gives up or runs code it wrote with its own trust.
- That code may have been steered by a hostile page or a poisoned file.
- Whatever it builds is thrown away: the next task starts from nothing.

---

## The idea

1. **Forge:** the agent submits a tool, or asks the server to build one.
2. **Verify:** the tool is tested, attacked and measured before anyone can use it.
3. **Reuse:** a person promotes it; any agent finds it, runs it in a sandbox, and trusts the record.

---

## How it works

- MCP server: a small, fixed set of meta tools. Nothing that approves or promotes is reachable from it.
- Registry: immutable versions, one state machine, frozen packages.
- Sandbox: a fresh hardened container per call, no network unless approved.
- Operator: the command line and a dashboard, separate from the agent's port.

---

## The gates

- **G1 Static check:** Reads the tool's files without running anything and looks for risky code, secret-shaped text and hidden instructions.
- **G2 Build:** Installs the tool's declared dependencies from ready-made wheels only and freezes everything into one sealed file.
- **G3 Self-tests:** Runs the tool's own tests and its manifest examples in a sandbox with no network, then checks that the tests really check something.
- **G4 Schema fuzz:** Feeds the tool generated inputs that are valid by its own input schema: empty text, long text, zero, big numbers, empty lists.
- **G5 Challenger:** A second AI model, not the one that wrote the tool, tries to break it with inputs the author did not think of.
- **G6 Containment:** Runs the tool with the network closed and decoy secrets planted (fake keys, an SSH key, an .env file), and watches what it touches.
- **G7 Approval:** A human operator decides on tools that ask for network access or secrets.

---

## Containment

- Hardened Docker containers from MiniLocker: read-only root, no capabilities, limits, no route out.
- Decoy credentials are planted; touching one fails the tool.
- A hash-chained ledger and a Blast Radius report say what happened, and what was *not measured*.

---

## The demo

- **Act 1, Forge:** The agent has no SQLite tool. It checks the shelf, forges one, the gates run, an operator promotes it, and the agent answers.
- **Act 2, Reuse:** A different question over a different database: the tool is found, not rebuilt, and the step count drops.
- **Act 3, Attack:** A poisoned file tells the agent to build a tool that steals credentials. The package is rejected and the ledger says why.
- **Act 4, Software and games:** The agent forges a session tool, plays a hand, takes a move back, and the session, snapshots and rewind are visible.

What the committed recording shows:

- **Act 1, Forge:** `sqlite_query` G1 to G4 passed, G5 to G6 skipped
- **Act 2, Reuse:** no gate ran (the shelf was used)
- **Act 3, Attack:** `expense_totals` G1 failed; `csv_totals` G1 to G4 passed, G5 to G6 skipped
- **Act 4, Software and games:** `blackjack_session` G1 to G4 passed, G5 to G6 skipped

Recorded 2026-11-28 with a scripted agent and the stand-in sandbox (no isolation); G5 was skipped.

---

## Benchmark

Not measured yet. Run `python -m bench --config all --runs 5 --out runs/bench/<timestamp>/ (docs/BENCHMARK.md)`; this slide fills in from its summary.json.

---

## Attack Lab

Not measured yet. Run `toolforge attacks run --json runs/attacks (docs/ATTACKS.md)`; this slide fills in from the result file.

---

## Limits, said plainly

- One egress allowlist for every tool
- The challenger can be wrong
- Static checks are advisory
- The container is not a virtual machine
- Containment evidence is a sample
- Python only

All of them: `docs/KNOWN_LIMITATIONS.md`.
