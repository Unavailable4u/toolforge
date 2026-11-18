# P18: Demo, packaging, project docs

Depends on: P15, P16, P17.

## Goal

Make the project understandable and reproducible by a stranger in ten minutes: a scripted demo that shows a real agent using
the server, one-command startup, and the documents that explain what it is, how it works and how to show it.

## Scope

`docker-compose.yml`, `demo/` (scripts, fixtures, recorded transcripts, one committed recording), `docs/{ARCHITECTURE.md,
PROJECT_SUMMARY.md,DEMO_SCRIPT.md,clients.md,KNOWN_LIMITATIONS.md,slides.md}`, `docs/assets/cover.svg` and a render script, final
`README.md` (replaces the starter text), tests for the demo's fixtures and for doc drift.

## One-command startup

`docker-compose.yml` runs: the ToolForge MCP server (HTTP transport), the dashboard, and a one-shot job that builds the
runtime image. The control plane needs the Docker daemon to create sandboxes, so mount access through a **socket proxy that
allows only the Docker API calls MiniLocker uses** (see MiniLocker's `deploy/` for the pattern) and never the raw socket. Both
listeners publish only on `127.0.0.1` and take their tokens from a git-ignored `.env`; compose fails with a clear message when
`TOOLFORGE_SERVER_TOKEN` or `TOOLFORGE_DASHBOARD_TOKEN` is missing. A `make demo-up` target and `docs/clients.md` configs for
common MCP clients (stdio command and HTTP URL with the bearer token).

## The demo (`demo/run_demo.py`)

A real agent (the benchmark's agent loop, or any MCP client the user prefers) is given tasks it has no tool for. Four acts,
each short enough to screen-record cleanly:

1. **Forge.** Task: answer a question over a SQLite file the agent has never seen. The agent calls `forge_find` (nothing),
   forges `sqlite_query`, the pipeline gates scroll by (G1 to G6), it is promoted, the agent uses it and answers.
2. **Reuse.** A second, different question over another database. `forge_find` returns the tool with high confidence; no new
   build; the step and token counts are shown next to Act 1.
3. **Attack.** A hostile page or file tells the agent to build a tool that reads credentials and sends them out. Show the
   package being rejected (or quarantined at runtime), the decoy event in the ledger, and the Blast Radius report with
   its "not measured" dimensions honestly labelled.
4. **Software and games.** The agent forges a session tool for a game or a format converter, plays or converts it, and the
   dashboard shows the session, snapshots and rewind.

`--replay demo/transcripts/` replays recorded model turns while still running the real server, sandbox and gates, so the demo
works without an LLM endpoint and without variance. The script says plainly when it is replaying. A `--live` mode uses the
configured endpoint. Seeds are fixed. The demo writes a `runs/demo/<timestamp>/` folder with ledgers and reports.

**`--record demo/recordings/<name>/`** additionally writes a small, sanitised recording: the ledger events of the run (allowlisted
fields only: event types, gate ids, statuses, counts, durations, truncated hashes, tool names), the transcript turn structure
without raw model text from tool output, `recording.json` (date, ToolForge version, seed, whether it was replayed or live), and
nothing else. One recording is **committed** to the repository so the website's tour (P20) shows real events without needing
Docker or a model. A test asserts a recording contains no environment values, no source code and no secret-shaped strings
(`verify.secretscan`).

## Documents

* `docs/ARCHITECTURE.md`: the final, polished version of the architecture (diagrams, lifecycle, gates, trust boundaries,
  data layout, the MiniLocker seam and what it contributes), accurate to the code.
* `docs/PROJECT_SUMMARY.md`: title, one-sentence description, the problem, the solution, how it works in five bullets, intended
  users and use cases, integrations (MCP clients, MiniLocker, Docker, any OpenAI-compatible endpoint), technology tags, links
  to the demo, repository and report. Short enough to paste into a form. P19 adds the website link.
* `docs/DEMO_SCRIPT.md`: shot list with timings for a 3 to 4 minute recording, exact commands and what to say, a fallback for
  each risky step, and a checklist for recording (clean terminal, dashboard layout, font size).
* `docs/slides.md`: a ten-slide outline in Marp-compatible Markdown (problem, idea, how it works, gates, MiniLocker, demo
  stills, benchmark results, attack results, limits, what is next). Numbers are filled from real result files by a small script.
* `docs/assets/cover.svg` plus `scripts/render_cover.py` (or a documented one-liner) producing a PNG; no stock imagery. The
  website uses the PNG as its social image.
* `docs/KNOWN_LIMITATIONS.md`: the honest list (global egress allowlist, challenger fallibility, advisory static checks,
  Python only, no pixel-level GUI control, cold start per call, single operator). **Strict format:** one bullet per limitation,
  `* **Short title.** One to three plain sentences.`, because the website (P19) parses this file. A test enforces the format.
* Final `README.md`: what it is, why it matters, a 60-second quick start, the demo, the architecture picture, the MCP tool table
  (drift-tested against `META_TOOLS`), the benchmark and attack summaries with links, the MiniLocker relationship and limits,
  license. The quick start includes the `toolforge promote` step for the no-LLM path.

## Tests

Demo fixtures are deterministic; replay transcripts parse and reference tools that exist; the committed recording validates and is
clean; README MCP tool table equals the real tool list; every document linked from the README exists; the compose file validates
(`docker compose config`, skipped without Docker) and refuses to start without tokens; `PROJECT_SUMMARY.md` stays under 400
words; `KNOWN_LIMITATIONS.md` matches its format.

## Acceptance

`make demo-up`, `python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/main/`, `pytest -q`.

## Pitfalls

Do not hard-code numbers in prose; read them from the latest results files or write "run the benchmark to see". Do not claim a
capability the demo does not show. Record nothing with real credentials on screen.
