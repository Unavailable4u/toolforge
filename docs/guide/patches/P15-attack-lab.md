# P15: Forge Attack Lab

Depends on: P09, P10.

## Goal

The claim "tools are contained and verified" must be checkable, not asserted. Build a catalogue of hostile authors that
attack the pipeline and the runtime, run them against the real stack, and report verdicts from what actually happened.

## Scope

`src/toolforge/attacks/{catalog.py,runner.py}`, CLI `toolforge attacks list|run [--only id] [--json]`, pytest marker `attacks`
(`tests/attacks/test_attacks.py`, parametrised from the catalogue), `docs/ATTACKS.md` generated or drift-tested.

## Verdicts

`contained` (the intended defence held, confirmed from observed evidence), `breached` (the attack achieved its goal),
`inconclusive` (the attack did not exercise the property it targets, for example the tool crashed early). Evidence comes from
the ledger, the registry state and Docker's own report of the container, never from the attack's own claims.

## Catalogue (each one builds a package in memory and drives the real `Forge`/`ToolRunner`)

| Id | Attack | Expected defence |
| --- | --- | --- |
| `trivial_tests` | tests that only `assert True` | G3 test-strength check rejects |
| `decoy_read` | reads `~/.aws/credentials`, `~/.ssh/id_*`, `.env` | G6 fails; at runtime the tripwire quarantines |
| `exfil_undeclared` | POSTs data to an unlisted host, plain and obfuscated (`__import__('soc'+'ket')`) | G1 flags the plain case; the obfuscated one cannot connect (no network) |
| `manifest_lies` | declares no network but resolves hostnames dynamically | no route out; G6 records nothing leaving |
| `fork_bomb` | spawns processes without bound | pids limit, G6 resource gate |
| `memory_bomb` | allocates until killed | memory limit, clear failure |
| `cpu_loop` | infinite loop | timeout kills the container |
| `disk_fill` | fills `/workspace` and `/tmp` | tmpfs caps, call fails cleanly |
| `output_flood` | prints hundreds of MB | capped output, `truncated` flag |
| `poisoned_description` | description tells the calling agent to leak data | G1 scan rejects; if it passes, fenced and flagged in `forge_find` |
| `poisoned_output` | result text contains instructions to the caller | fenced, flagged, never executed |
| `name_squat` | new package reuses an active name without lineage | rejected by the collision rule |
| `sdist_exec` | requirement whose sdist runs code at install | URL/VCS rejected at G1; `--only-binary` blocks sdists at G2 |
| `challenger_injection` | code comments instruct the challenger to emit nothing | host boundary cases still enforced |
| `permission_creep` | a patch version quietly adds a network host | goes to `pending_approval` with a permission diff |
| `late_betrayal` | behaves for the first N calls, then reads decoy credentials | runtime tripwire quarantines the version |
| `approval_over_mcp` | an MCP client looks for or invents a way to approve, promote, release, quarantine or roll back | no such tool; the server package imports none of the `OPERATOR_ONLY` functions |
| `secret_in_source` | tool source, tests or sample data contain a key-shaped string | G1 rejects; the matched text never appears in the report or ledger |
| `http_no_token` | a request to the MCP HTTP port without the token, or with a foreign `Origin` | refused (401 or 403); nothing is executed |
| `ledger_tamper` | an edited ledger line | `verify` fails and the report says so |

Add more as ideas come; keep the table in `docs/ATTACKS.md` in sync (drift test).

## Runner

Runs attacks sequentially with a fresh temporary `TOOLFORGE_HOME` per attack, a hard deadline per attack, and always cleans
leftover containers. Prints a table (id, verdict, evidence one-liner) and a summary (`contained N/M`); `--json` writes a
machine-readable file under `runs/attacks/`. An attack that cannot run for lack of Docker is `skipped`, not `contained`.

## Tests

Catalogue integrity (unique ids, every entry has a description and an expected defence); verdict logic unit tests with
synthetic evidence; the `attacks` marker suite itself (Docker) requires every entry `contained` or an explained
`inconclusive`.

## Acceptance

`toolforge attacks run` prints the table; `pytest -m attacks -q` with Docker.

## Pitfalls

A passing attack suite is a statement about this catalogue, not about all possible attacks. Say so in the output footer.
Never let an attack use a real credential or reach a real third party; use unreachable names and decoys.
