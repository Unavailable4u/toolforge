# ToolForge build guide

## What we are building

ToolForge is an MCP server with one purpose: when an AI agent lacks a tool for a task, it can safely create one, prove
it works, and reuse it later.

**The problem.** Agents fail on tasks that need a capability nobody pre-built: parsing an odd file format, querying a
specific database, operating a program or game. Today either a human writes the tool, or the agent runs arbitrary code
with no record of what it did and no memory for next time.

**The idea.** Treat tool creation as a supply chain with gates. A tool is code plus tests plus a manifest of permissions.
It is built, then verified by a pipeline that includes an independent reviewer and a containment probe, then published
with a version and a tamper-evident history. Running a tool always happens inside a hardened sandbox, with only the
permissions its manifest declares and a human has approved.

**What makes it different from "an MCP server that runs generated code".**

1. Least privilege by construction: each tool declares network hosts and secrets; the sandbox enforces them.
2. Verification before publication, including an independent challenger and a containment probe with decoy credentials.
3. Provenance: every version has a hash-chained history and a Blast Radius report measured from what actually happened.
4. Reuse with repair: tools are searchable, scored by real outcomes, repaired through the same gates, and roll back.
5. Software use: adapter kits let the agent forge tools that query databases, convert file formats, drive a browser, and
   play state-based games, in tiers with honest limits.
6. Measured, not claimed: a benchmark and an attack suite produce numbers with confidence intervals.
7. Explained: a plain-language website with a guided tour, built from the same sources as the code, so what it says is what
   the software does.

## Non-goals

* Not a general "run any code" server. Tools are packaged, versioned and permissioned.
* Python tools only in v1. No GUI automation guarantees (an experimental stretch kit exists in P14).
* Not multi-tenant. One operator, one registry.
* Not a replacement for the container as the security boundary. Static checks are early warnings; the sandbox is the wall.

## The patches

Read `PROGRESS.md` for status and `05-milestones.md` for the route. Each patch has its own file in `patches/`.

| Patch | File | One line |
| --- | --- | --- |
| P01 | `P01-skeleton.md` | Package, CLI shell, config, CI |
| P02 | `P02-tool-package-spec.md` | The tool package format and its validation |
| P03 | `P03-registry.md` | Versioned registry with lifecycle, blobs, search |
| P04 | `P04-sandbox-seam.md` | Sandbox interface, fake runner, MiniLocker runner |
| P05 | `P05-execution-contract.md` | How a tool runs inside a sandbox; the `run` path |
| P06 | `P06-verification-pipeline.md` | Gates G1 to G4 |
| P07 | `P07-mcp-server.md` | The MCP server, its meta-tools, and operator `promote` |
| P08 | `P08-containment-and-blast-radius.md` | Gate G6 and the per-tool report |
| P09 | `P09-permissions-and-approvals.md` | Manifests enforced, approvals, egress, brokered secrets |
| P10 | `P10-challenger.md` | Independent verification, gate G5 |
| P11 | `P11-builder.md` | Server-side tool builder |
| P12 | `P12-reuse-and-repair.md` | Ranking, self-repair, rollback |
| P13 | `P13-sessions.md` | Stateful tools |
| P14a | `P14a-runtime-image-and-kits.md` | Runtime image, recipes and software-use packs |
| P14b | `P14b-browser-plans.md` | Browser plans and the GUI kit |
| P15 | `P15-attack-lab.md` | Hostile-author attack suite |
| P16 | `P16-benchmark.md` | Benchmark harness |
| P17 | `P17-dashboard.md` | Web dashboard |
| P18 | `P18-demo-and-docs.md` | Demo, packaging, project docs |
| P19 | `P19-landing-site.md` | The landing site: explanation, architecture, evidence, GitHub button |
| P20 | `P20-guided-tour.md` | The interactive tour, driven by a recorded real run |
| P21 | `P21-hardening-and-release.md` | Final pass and release |
| S01 | `S01-bundles-and-attestations.md` | Optional: portable bundles and signed provenance |
| S02 | `S02-chains.md` | Optional: declarative tool chains |

## Other documents in this folder

| File | What it is |
| --- | --- |
| `01-conventions.md` | How code, settings, errors, tests and ledgers are written here |
| `02-architecture.md` | Components, trust boundaries, lifecycle, gates, calls, events |
| `03-minilocker-integration.md` | What is reused from MiniLocker and how |
| `04-optional-resources.md` | Optional models and tools (Gemini, Gemma and so on) and how they fit |
| `05-milestones.md` | Milestones, rough sizes, cut lines |
| `06-wsl2-setup.md` | Setting up WSL2 (Ubuntu), Docker and Python 3.12, and applying patches from Windows |
| `site-brief.md` | Audience, plain-language rules, page map and design direction for the website |
| `package-format.md` | The tool package format (rewritten as a reference in P02) |
| `REVISIONS.md` | How this guide changed and why |
| `DEVIATIONS.md` | Created when reality differs from the guide (not present at the start) |

## Keeping the guide consistent

`python3 scripts/check_guide.py` checks that every patch has a progress row and a file, that each "Depends on" line matches the
table, that titles agree, that no patch is missing a required section, that every file named in the index exists, that every `Pnn`
mentioned anywhere is a real patch and that no file has Windows line endings. Run it after editing any guide file. P01 wraps it in
a test (`tests/test_guide_consistency.py`).

## Order of reading for a newcomer

`01-conventions.md` (how code is written here), `02-architecture.md` (components, trust, lifecycle),
`03-minilocker-integration.md` (what is reused and how), `05-milestones.md` (the route), then the patches in order.
