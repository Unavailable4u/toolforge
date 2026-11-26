# Declarative tool chains (S02, optional)

`forge_chain` runs a short, fixed list of **active, verified** tools in order, in one call, and lets a step use a value an earlier
step returned. "Find rows, summarize, format" then costs one round trip instead of three. It is off by default.

A chain is **data, not a program**. A step has an `id`, a `tool` and `args`; a value in `args` is a literal JSON value or a reference
to an earlier step's output. There is no expression, template, condition, loop or function, no `script` or `code` field, and a tool
cannot call another tool. Every step is an ordinary `forge_run`: its own sandbox, its own limits, its own ledger events, its own
statistics. A chain cannot be saved as a composite tool.

## Turn it on

| Setting | Default | Meaning |
| --- | --- | --- |
| `TOOLFORGE_CHAIN_ENABLED` | `false` | When `false`, the server does not list `forge_chain` and a call is refused with `chain_disabled`. |
| `TOOLFORGE_CHAIN_MAX_STEPS` | `5` | Steps per chain, 1 to 8 (8 is a ceiling in the code that no setting can raise). |
| `TOOLFORGE_CHAIN_TOTAL_SECONDS` | `60` | After this long, no further step is **started**; a step that is already running keeps its own limit and is not interrupted. |

## The request

```json
{"steps": [
  {"id": "a", "tool": "csv_summary", "args": {"text": "..."}},
  {"id": "b", "tool": "format_table", "args": {"rows": {"$ref": "a.output.rows"}, "style": "plain"}}
]}
```

(The tool names are examples; use `forge_find` to see the tools you have.)

* `steps` has 1 to `chain_max_steps` entries. `id` is unique and matches `^[a-z][a-z0-9_]{0,15}$`. `tool` is a tool name with no
  version: its **active** version is looked up once when the chain starts and used for the whole chain, so a promote in the middle changes
  nothing that runs. (A version that is deprecated in the middle still finishes the chain; one that is quarantined stops it at that step.)
* A reference is an object with exactly one key, `{"$ref": "<step id>.output.key.0.key"}`: an **earlier** step's id, `.output`, then
  zero or more `.key` or `.index` parts. A key is a letter, then letters, digits or `_` (so `__proto__` and a key made of digits can never
  be named); an index is `0`, `1`, `2`, ... with no sign and no leading zero. No brackets, quotes, `..`, wildcards or leading `/`.
* A reference is resolved once, just before its step runs, by **copying** a JSON value (at most 64 KiB) out of the earlier step's
  recorded output. A path that does not exist is an error that names the step; it is never `null`. `args` itself may be a reference when
  an earlier step returned the whole argument object.
* A `$ref` is read only in the request's `args`. A `$ref` that appears inside a tool's output is data and is never followed.

## Which tools can be chained

A tool is eligible only if its active version asks for no network hosts, no secrets, no files and no browser, keeps no session (a browser plan, P14b, is its own kind and is refused too), and has
the stored package and passed build gate (G2) that `promote` requires. Every step is checked **before step 1 runs**; a refusal names every
step that cannot be chained and why (`chain_not_eligible`), and no sandbox has been opened. A chain therefore cannot be a way around
[approvals](approvals.md): network and secrets are granted there, and no step can ask for either.

## The response

One response, in one random fence for the whole chain. Per step: `id`, `tool` (`name@version`), `status` (`ok`, `failed`, `skipped`),
`run_id` and `duration_ms` (absent when nothing ran for that step), the fenced `output` or the `error`, `stdout`/`stderr` when there
are any, `timed_out`, `truncated` and the step's own `flags`. For the chain: `ok`, `stop_reason` (`step_failed`, `time_budget` or
`null`), `duration_ms`, `flags`, `injection_scan`.

* The first failing step stops the chain; the rest are `skipped` and have no output. A step whose arguments do not match the tool's
  `input_schema`, or whose reference points at nothing, fails without running (no `run_id`).
* Every output is tool output and is **hostile text**: cleaned, capped (8000 characters, with `truncated` set when cut), scanned for
  injection (`possible_injection` on the step and on the chain, when the scanner is available; otherwise `injection_scan` says
  `not measured`) and wrapped in the same `[UNTRUSTED TOOL DATA ...]` fence as `forge_run`. The server never puts an output in a prompt, a
  tool description or a `$ref` position.
* A request that is not a chain (`bad_chain`) or a tool that cannot be chained (`chain_not_eligible`) is a tool error, written by
  ToolForge, before anything runs.

## Audit

`ledgers/chains/<chain id>.jsonl` gets `forge.chain.started` (chain id, caller, limits, and for every step its id, tool, version, code hash
and how many references it holds) and `forge.chain.finished` (outcome, stop reason, and for every step its id, tool, status, run id,
and for an `ok` step the SHA-256 and size of the output). It never holds an output, an argument or an error text. Each step also writes its
normal `forge.call.started` and `forge.call.finished` events to the tool's ledger with `chain_id` added, so one chain can be followed
from either side.

## Limits that are written down

* A chain does not branch, loop, retry or run steps in parallel. If a real need for a condition appears it is a new patch with a new threat
  model, not an option here.
* There is no per-caller rate limiter in this repository yet. Each step is an ordinary call (its own sandbox, its own statistics), so any
  limiter placed in `ToolRunner.run` counts every step; none is claimed today.
* The response can hold up to `chain_max_steps` outputs of 8000 characters each. A large output is cut, not paged.
* Not measured: the bytes and tokens a chain saves compared with the same steps as separate calls (the benchmark patch, P16, has not been
  built).
* Tested with a scripted sandbox (no tool code runs) over the real registry and the real runner (`tests/test_chain.py`,
  `tests/test_chain_server.py`). Not run: a chain of real tools in real containers.
