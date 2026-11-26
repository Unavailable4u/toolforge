# Connecting an MCP client

ToolForge speaks the Model Context Protocol. `toolforge serve` is the server; any MCP client can use it. This page says how to start
it, how to point a client at it, what the tools are, and what keeps it safe.

The server side is tested with the MCP SDK's own client (in memory, over a real local HTTP port, and over a real stdio subprocess; see
`tests/test_server_tools.py` and `tests/test_server_http.py`). The configuration snippets for particular client programs below are
**not** tested against those programs: client menus and file names change, so check your client's own documentation if one does not work.

## 1. Make a tool usable (once)

A tool an agent submits is verified, but it does not become usable until an operator says so. With no language model configured it
stops at `verified_basic`; you make it active yourself:

```
toolforge submit examples/toolpacks/csv_summary
toolforge promote csv_summary@1.0.0 --reason "local dev"
```

`promote` is a command for you, not a tool for agents: the server cannot reach it. It refuses versions that ask for network access or
secrets (those wait as `pending_approval` for `toolforge approvals approve`, see [approvals](approvals.md)), versions that were not built,
and anything that is not `verified_basic` or `verified`.

## 2. Start the server

**stdio** (the client starts the command and talks over its pipes; nothing listens on the network):

```
toolforge serve
```

**HTTP** (a long-running local server):

```
toolforge serve --transport http            # 127.0.0.1:8765, endpoint /mcp
toolforge serve --transport http --port 9000
```

HTTP rules, enforced by `toolforge.netguard` (the same code the dashboard uses):

* The server binds `TOOLFORGE_SERVER_HOST` (default `127.0.0.1`). Any other address needs `TOOLFORGE_SERVER_TOKEN`, or it **refuses to start**.
  An empty value does not count as a token.
* With a token, every request must send `Authorization: Bearer <token>`; anything else gets a bare 401. The token is read from the
  environment only (never from the command line) and is never logged or printed.
* The `Host` header must be one the server expects and an `Origin` header, if present, must be a loopback origin; anything else gets a bare
  403. This stops a web page from reaching a local server through DNS rebinding. Behind a proxy or under another name, list the
  extra values in `TOOLFORGE_SERVER_ALLOWED_HOSTS` and `TOOLFORGE_SERVER_ALLOWED_ORIGINS` (comma separated).
* There is no encryption. Beyond a trusted network, put a TLS-terminating proxy in front, and remember that a loopback address protects
  against other machines, not against other users or programs on this one.

## 3. Point a client at it

Claude Code:

```
claude mcp add toolforge -- toolforge serve
claude mcp add --transport http toolforge http://127.0.0.1:8765/mcp --header "Authorization: Bearer $TOOLFORGE_SERVER_TOKEN"
```

A client with a JSON configuration file (Claude Desktop, Cursor and others use this shape; the file location differs):

```json
{
  "mcpServers": {
    "toolforge": {
      "command": "toolforge",
      "args": ["serve"],
      "env": { "TOOLFORGE_HOME": "/home/you/.toolforge" }
    }
  }
}
```

If `toolforge` is not on the client's `PATH` (a virtual environment), use the full path to the executable as `command`. A client that
supports remote servers takes the URL `http://127.0.0.1:8765/mcp` and the header `Authorization: Bearer <token>`.

## 4. The tools

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
| `forge_chain` | `steps: [{id, tool, args}]` | per step `status` (`ok`, `failed`, `skipped`), `run_id`, `duration_ms` and the fenced `output` or `error`; `stop_reason`, `flags` | S02 |
<!-- tools:end -->

`forge_chain` is an optional tool: the server offers it only when `TOOLFORGE_CHAIN_ENABLED=true`. It runs a short fixed list of active
tools in one call (no network, no secrets, no files); see [chains](CHAINS.md).

There is no tool that approves, promotes, releases, rolls back or deletes anything. Those are operator actions.

What an agent should expect:

* `forge_find` lists only **active** tools, best match first. `confidence` says how much of the query's words the tool's name and
  description cover, and is lowered for a tool whose recent calls mostly fail; it is not a quality score. `why` is one line of numbers.
  When the best match is `low`, the answer says to build or submit a tool instead. `forge_describe` also returns `signature`, the exact
  call shape as a Python and a TypeScript stub built from the schemas alone (it describes the call; it is not code to import). See `docs/repair.md`.
* `forge_run` runs only active versions. Any other state returns an error that names the state and the next step.
* Everything a tool printed or returned arrives **fenced**: between markers that carry a random id that changes with every response, under a
  line that says it is data and not instructions. Text that looks like an instruction is flagged (`flags: ["possible_injection"]`) and
  left unchanged. `injection_scan` says `measured` or `not measured`.
* Descriptions and manifest text written by tool authors are cleaned of invisible and control characters and capped (600 characters).
* `forge_submit` reports progress for each gate. If the server answers *busy*, wait a minute and submit the same package again; that is safe.
* `forge_build` is for a client that cannot or does not want to write a tool: it says what it needs in words, and the server's own model
  writes the package. It reports progress for each round and gate, and it can take minutes. A tool that is built is not usable until an
  operator promotes it (or approves the hosts or secrets it asks for); the result's `status` and `next_action` say which. See `docs/builder.md`.
* `forge_repair` writes a new version of a tool that fails (the server's model, or a package you send) and verifies it like any
  submission, with the stored regression cases. The old version stays active until an operator promotes the new one. Repairs are limited
  per tool per hour. `forge_report_problem` only tells the operator: a report never pulls a tool, and `forge_status` shows its count,
  not its text. See `docs/repair.md`.
* A tool with `kind: "session"` keeps state between calls. `forge_run` without `session_id` opens a session and returns its `session_id`; pass
  it on later calls. Keep the whole id private: it is the only proof the session is yours. `forge_session_rewind` and `forge_session_close` act only on
  your own session, and an idle session closes after 15 minutes. See `docs/sessions.md`.
* Errors are tool errors with a message, a hint and a short `ref`. The same `ref` is in the server's log (stderr), where the details are.

## 5. Optional: every active tool as its own tool

Set `TOOLFORGE_EXPOSE_DIRECT=true` and each active tool is also offered as `tf_<name>`, with its own input schema, so a client can call it
without `forge_find` and `forge_run`. A call is exactly a `forge_run`. Tools that take files are not offered this way. The server checks
the registry every two seconds and sends a tools-list-changed notification when a tool is promoted, deprecated or quarantined (by you, in
another process). Clients on the 2026-07-28 protocol receive it through `subscriptions/listen`; clients on the 2025-11-25 protocol see the
change the next time they list tools.

## 6. Settings

| Setting | Meaning |
| --- | --- |
| `TOOLFORGE_SERVER_HOST`, `TOOLFORGE_SERVER_PORT` | where `toolforge serve --transport http` listens (127.0.0.1, 8765) |
| `TOOLFORGE_SERVER_TOKEN` | required for any host that is not loopback; sent by clients as a bearer token |
| `TOOLFORGE_SERVER_ALLOWED_HOSTS`, `TOOLFORGE_SERVER_ALLOWED_ORIGINS` | extra `Host` and `Origin` values to accept |
| `TOOLFORGE_EXPOSE_DIRECT` | offer active tools as `tf_<name>` |
| `TOOLFORGE_MAX_CONCURRENT_VERIFICATIONS` | how many `forge_submit` verifications run at once; more wait up to 30 seconds, then get *busy* |
| `TOOLFORGE_LLM_BASE_URL`, `TOOLFORGE_LLM_MODEL`, `TOOLFORGE_LLM_API_KEY` | the builder model that `forge_build` asks; without them `forge_build` can only reuse an existing tool |
| `TOOLFORGE_BUILDER_MAX_ROUNDS`, `TOOLFORGE_BUILDER_TOKEN_BUDGET` | how many times, and with how many model tokens in all, one build (or one repair) may ask for a package (4 and 60000) |
| `TOOLFORGE_REPAIR_MAX_PER_HOUR` | how many `forge_repair` attempts may start on one tool in an hour (3) |
| `TOOLFORGE_STORE_FAILED_INPUTS` | keep the raw arguments of failed calls as regression cases (false); see `docs/repair.md` before turning it on |

Docker must be running for `forge_run`, `forge_submit`, `forge_build` and `forge_repair`. Without it the server still starts and answers `forge_find`, `forge_describe`,
`forge_list`, `forge_status`, `forge_recipe` and `forge_runtime_info` (which says "not measured" for what it cannot read).
