# ToolForge

**An MCP server that lets an AI agent build, verify and reuse its own tools, safely.**

Agents get stuck the moment a task needs a capability they were not given: a file format nobody wrote a parser for, a
database they have no query tool for, a program they cannot drive. ToolForge closes that gap. When an agent has no tool
for the job, it asks ToolForge to forge one. The tool is written, tested, attacked, measured and only then published
into a registry where this agent, and any other MCP client, can find and reuse it. Every tool runs inside a hardened
sandbox, always, and carries a signed-off list of what it is allowed to touch.

> Status: this repository is being built patch by patch from the guide in `docs/guide/`. This README is replaced by the
> real one in patch P18, and P19 adds the link to the project website. Until then it explains the workflow.

## How the pieces fit

```
 any MCP client (Claude Desktop, an IDE, your own agent)
        |  MCP (stdio or streamable HTTP)
        v
 +---------------------------+        +---------------------------------------------+
 |  ToolForge MCP server     |        |  MiniLocker (pinned dependency)             |
 |  find / describe / run    |  uses  |  hardened Docker sandboxes, egress proxy,   |
 |  submit / build / repair  +------->|  credential broker, decoys, hash-chained    |
 |                           |        |  ledger, Blast Radius report                |
 |  verification pipeline    |        +---------------------------------------------+
 |  G1..G7 gates             |
 |  registry (SQLite + blobs)|
 +---------------------------+
```

* **Forge**: the agent submits a tool package (code, tests, manifest) or asks the server to build one from a goal.
* **Verify**: seven gates (static checks, build, self-tests, schema fuzzing, an independent challenger, containment
  probing, approval) decide whether the tool is published. Every gate writes to a tamper-evident ledger.
* **Run**: tools execute only inside a fresh MiniLocker sandbox with exactly the permissions their manifest declares.
* **Reuse and repair**: tools are versioned, searchable, scored by real outcomes, and repaired through the same gates.

## Start here (first ten minutes)

1. Create an empty repository on GitHub (private is fine at first).
2. In this folder:
   ```
   git init -b main
   git config core.autocrlf false
   git add . && git commit -m "Starter: guide and plan (revision 3)"
   git remote add origin <your repository url>
   git push -u origin main
   ```
3. Install Python 3.12 and Docker. On Windows work inside WSL2 (Ubuntu), keeping the repository on the Linux side. The step by
   step is in `docs/guide/06-wsl2-setup.md`. Unit tests need no Docker; real sandboxes do.
4. Check the plan is consistent: `python3 scripts/check_guide.py` should print "guide is consistent".
5. Skim `docs/guide/00-index.md`, then `docs/guide/05-milestones.md` to see the route and where you can stop.
6. Open the repository with a coding agent and ask for the first patch (prompts below).

You do not need any API key to begin. A language model is needed only for the challenger gate, the builder, the benchmark and
the live demo (see `docs/guide/04-optional-resources.md`; every resource listed there is optional).

## Connect an MCP client

`toolforge serve` runs the MCP server (stdio by default, `--transport http` for a local HTTP listener). After `toolforge submit <dir>`
verifies a tool, the operator runs `toolforge promote <name>@<version> --reason "..."` to make it active. Setup, client
configuration, and the security rules are in `docs/clients.md`. How tools are ranked, described, repaired and rolled back is in
`docs/repair.md`.

<!-- tools:begin -->
| Tool | Input | Returns | Since |
| --- | --- | --- | --- |
| `forge_find` | `query: str`, `limit: int = 5` | ranked active tools with `confidence`, a one-line `why`, `uses`, `success_rate` (or null) and permissions | P07 |
| `forge_describe` | `name: str`, `version: str | null` | the sanitised manifest, a typed call `signature`, examples, status, lineage and gate summaries | P07 |
| `forge_run` | `name: str`, `args: object`, `files: object | null`, `session_id: str | null` | the call result: `ok`, the fenced `output` or `error`, `stdout`, `stderr`, `flags`; for a session tool also `session_id`, `step` | P07 |
| `forge_submit` | `package: {manifest, files}` | the verification report: status, gates with summaries and hints, `next_action` | P07 |
| `forge_status` | `name: str`, `version: str | null` | status, `next_step`, gate summaries and approval state | P07 |
| `forge_list` | `status: str | null` | versions with name, version, status and a short description (capped) | P07 |
| `forge_recipe` | `kind: str` | the documentation text (a recipe per kind of tool; an unknown kind lists them) | P07 |
| `forge_runtime_info` | none | Python version, runtime image, sandbox limits, preinstalled libraries (or null: not measured) | P07 |
| `forge_build` | `goal: str`, `examples: [{input, output?}] | null`, `sample_files: object | null`, `constraints: str | null` | `reused` or the built `tool` and `version`, its `status`, `rounds`, `usage`, the verification `report` and `next_action` | P11 |
| `forge_repair` | `name: str`, `problem: str`, `package: {manifest, files} | null` | `ok`, the new `version` and its `status`, `rounds`, the verification `report` and `next_action` | P12 |
| `forge_report_problem` | `name: str`, `version: str | null`, `description: str` | `stored`, the report id and what happens next | P12 |
| `forge_session_close` | `session_id: str` | `closed`, the number of `steps`, the sandbox audit result and a one-line Blast Radius `verdict` | P13 |
| `forge_session_rewind` | `session_id: str`, `step: int` | the `step` it is at now and the `steps_available` that can still be restored | P13 |
| `forge_session_list` | `session_id: str | null` | open sessions by public name (tool, steps, time left); with your `session_id`, its `steps_available` too | P13 |
<!-- tools:end -->

## Working on this repository with an AI agent

The build plan is split into patches. Give any coding agent the repository link and a prompt such as:

> Read `AGENTS.md` and give me the patch file for the next unfinished patch.

or name one: "Read `AGENTS.md` and generate patch P05." The agent returns a `.patch` file that you apply with
`git am`. Progress is tracked in `PROGRESS.md`.

Other prompts that work:

* A patch is too big for one pass: "Read `AGENTS.md`. P06 looks too big: propose how to split it, then generate P06a."
* Something broke: "Read `AGENTS.md`. Patch P05 is applied but `<command>` fails with `<output>`. Generate a fix patch."
* An optional patch: "Read `AGENTS.md` and generate S01." (or S02, declarative tool chains).

If `git am` fails, run `git am --abort`, make sure the working tree is clean and `core.autocrlf` is `false`, and retry. Use
`git am --3way` only after reading what conflicted.

## The plan, the site and the optional pieces

* **Milestones and cut lines:** `docs/guide/05-milestones.md` groups the patches into milestones that each end with something
  you can run, and says honestly what can be dropped.
* **The website (P19, P20):** a plain-language landing page with a guided tour, an architecture explanation, the gates, the
  security model and evidence, a GitHub button top right, deployed to GitHub Pages. The brief is `docs/guide/site-brief.md`.
* **Working in WSL2 on Windows:** `docs/guide/06-wsl2-setup.md` (Python, Docker, git settings, and how to apply a patch you
  downloaded in the browser).
* **Optional resources:** `docs/guide/04-optional-resources.md` (Gemini through the OpenAI-compatible endpoint, Gemma,
  and so on). Nothing depends on them.
* **What changed in this revision of the plan, and why:** `docs/guide/REVISIONS.md`.

## Repository map (final layout)

```
src/toolforge/        the server, registry, verification pipeline, sandbox adapter, CLI
tests/                unit tests (no Docker) and marked docker / llm / attacks / browser suites
examples/toolpacks/   reference tool packages
bench/                the benchmark and its task set
demo/                 the scripted demo and its committed recording
site/                 the landing site: sources, data snapshot, build output (site/dist is ignored)
scripts/              guide checker (present from the start), site build and data scripts (P19, P20)
third_party/minilocker  pinned git submodule (external dependency, never copied)
docs/guide/           the build guide (this is where the patches are specified)
```

## License

MIT (added in patch P01).
