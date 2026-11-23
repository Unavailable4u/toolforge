# The server-side builder

A client that cannot, or does not want to, write a tool says what it needs in words. The server's own model writes a package, the
verification pipeline checks it, and a failure goes back to the model until a package passes or the build gives up.
It is the `forge_build` MCP tool and the `toolforge build "<goal>"` command, and both run the same code
(`src/toolforge/services/builder.py`).

```
toolforge build "count the words in a text" --examples examples.json --sample sample.txt
```

## What you need

* A builder model: any OpenAI-compatible chat endpoint (`TOOLFORGE_LLM_BASE_URL`, `TOOLFORGE_LLM_MODEL`, and `TOOLFORGE_LLM_API_KEY` if it
  needs one). `toolforge doctor` shows whether it is configured; it makes no request to the model.
* Docker, because the package is built and tested in a sandbox like any submission.
* Without a model the builder still does its first step, reuse. It then stops and says that no model is configured.

## What happens

1. **Reuse first.** The goal is searched among the *active* tools. A match whose `confidence` is `high` (the same simple measure
   `forge_find` shows: the share of the goal's words found in the tool's name and description; it is not a quality score) is returned
   with `reused: true` and nothing is built.
2. **Draft.** The model is shown the package format (the `package-format` recipe), the facts about the sandbox (Python version, limits and
   installed libraries from the runtime image, or "not measured"), and the request. Everything the caller wrote is fenced as untrusted
   data. The goal, constraints, examples and sample files are scanned for secret-shaped text first; a hit refuses the request before
   anything is sent, and the model client scans the whole prompt again.
3. **Verify.** The package goes through `Forge.submit`, as the author `builder`, with the same gates and the same limit on concurrent
   verifications as any other submission. On a failure the model gets back only the first failed gate's id, name, summary and hints, and its
   previous package. It never sees a raw log.
4. **Stop.** A build ends on the first of these (`stop_reason`):

| `stop_reason` | When |
| --- | --- |
| `verified` | the pipeline did not reject the package |
| `reused` | an active tool already fits the goal |
| `max_rounds` | `TOOLFORGE_BUILDER_MAX_ROUNDS` (4) rounds, none passed |
| `stuck` | two rounds in a row failed the same gate with the same summary, or the model twice returned something that was not a package |
| `token_budget` | the tokens spent, plus the size of the next prompt, reach `TOOLFORGE_BUILDER_TOKEN_BUDGET` (60000) |
| `llm_unavailable` | the model provider cannot be used (not configured, unreachable, rate limited past the client's wait, key refused); it never loops |
| `llm_refused` | the prompt held secret-shaped text, so it was not sent |
| `busy` | the verification slots stayed busy |
| `name_conflict` | the package could not be registered |

## Who decides what

The builder model is **another untrusted author**. Its package gets no shortcut and no status of its own:

* The loop, not the model, sets `name`, `version` and `parent_version`. The first version is `0.1.0`, and each further registered round is
  the next patch number, with the previous round as its parent. When the name the model chose is already in the registry, the build uses a
  free variant (`name_2`) and says so in `name_adjusted`. A build can never replace or collide with an existing tool.
* Every version a build registers is created by `builder` and stays in the registry, rejected ones included. History is evidence.
* The builder cannot promote, approve, release, quarantine or roll back anything (`tests/test_builder.py` reads its source to check). A tool
  that passes ends where the pipeline ends it: `verified_basic` when a gate could not run (for example no challenger model, so G5 is
  skipped), `verified`, or `pending_approval` when the manifest asks for network hosts or secrets. `next_action` says who acts next;
  usually the operator runs `toolforge promote <name>@<version> --reason "..."`, or `toolforge approvals ...`.
* Model output is data. It is validated and submitted, never run on the host.
* The prompt tells the model not to ask for network or secrets unless the goal needs an external service. The prompt is not the control:
  the gates and the approval step are, and a package that asks anyway waits for a human.

## Cost and records

The result reports rounds, model requests, tokens, the budget used and wall time. A provider that reports no token counts gives
`null` for `tokens_in` and `tokens_out` ("not measured"), and the budget is then counted with an estimate of about four characters a token,
labelled `budget_basis: estimated`.

Each build writes one event, `forge.build.finished`, to `$TOOLFORGE_HOME/ledgers/_builds/<build id>.jsonl`. It holds ids, hashes,
counts and labels: never the goal, a prompt, code or text from the model. A build that ends by an exception (the sandbox is down) writes
the event with `outcome: aborted`. When the ledger cannot be written the result carries a warning; the registry is still right.

## Limits

* How often a build succeeds, and at what cost, is **not measured** yet; the benchmark (P16) measures it.
* No live model run was made when this was written. The loop is tested with a scripted model (`FakeLLM`) against a real registry and
  scripted gates, and `toolforge build` is tested with the real gates G1 to G4 on a package the scripted model returns. What a real
  model writes, and how often it passes, is not measured.
* The challenger (G5) is a second opinion from a model. When it is the same model as the builder, it tends to miss the same things;
  see `docs/guide/04-optional-resources.md`.
* Every build in a process shares one rate limiter for the builder model (`TOOLFORGE_LLM_MAX_RPM`); the challenger has its own. A free tier that
  keeps answering HTTP 429 ends a build as `llm_unavailable`, after the client's retries and backoff.
