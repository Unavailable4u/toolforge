# The Forge Attack Lab

ToolForge claims that tools are contained and verified. The attack lab makes that claim checkable: it builds hostile tool packages
(a catalogue of twenty-seven "hostile authors"), drives them through the real pipeline, runtime, MCP server and ledger, and reports what
actually happened.

```
toolforge attacks list                 # the catalogue
toolforge attacks run                  # run everything; prints a table and a summary
toolforge attacks run --only decoy_read --only cpu_loop
toolforge attacks run --json           # also writes runs/attacks/attacks-<time>.json
pytest -m attacks -q                   # the same, as a test suite (needs Docker and the built runtime image)
```

A passing attack suite is a statement about this catalogue, not about all possible attacks.

## Verdicts

| Verdict | Meaning |
| --- | --- |
| `contained` | The intended defence held, and the observations listed for the attack prove it. |
| `breached` | The attack achieved its goal: at least one observation showed the defence not holding. |
| `inconclusive` | The attack did not exercise the property it targets (it was stopped earlier, nothing could be observed, or it ran past its deadline), so nothing can be claimed. |
| `skipped` | It could not run here: no Docker daemon, no runtime image (`toolforge images build`), or no MiniLocker. A skipped attack is never counted as contained. |

Evidence is what the ledger, the registry and Docker's own report of the container say, never what the attacking tool says. A tool's
own output is used at most to show that it *attempted* something. Each attack has its own checks (see `src/toolforge/attacks/catalog.py`)
and a check is `pass`, `fail` (breached), `unproven` (inconclusive) or `skip`. The verdict is `breached` if any check failed, `inconclusive` if any
is unproven or none passed, and `contained` otherwise.

Every attack also gets three run-wide checks, so a breach the attack did not aim at is still seen:

* a **host canary** (a file and an environment variable on this machine) is intact and its random token appears in no file of the
  attack's data directory, so no host secret reached a ledger or the registry;
* every ledger the attack produced verifies as a hash chain (the `ledger_tamper` attack edits one on purpose and is exempt);
* every sandbox the ledgers say was created was destroyed, and Docker lists no sandbox container the attack left behind. Leftover
  containers are removed after each attack and reported.

## How a run works

Attacks run one at a time. Each gets a fresh temporary `TOOLFORGE_HOME`, a hard deadline (default 300 seconds, `--deadline`), and its
own registry. A hostile package is built in memory and only ever runs inside a real hardened sandbox. The exit code is `1` if any attack
was breached, `2` if none was but something is inconclusive or skipped, and `0` when everything that ran was contained.

Victim tools (the active tool a squatter targets, say) are put in the registry with scripted gates (`SeedGate`, whose summary says so).
No verdict rests on a seeded gate: where an attack is about a gate, the real gate runs.

## The catalogue

The table is generated from the code (`toolforge attacks list --markdown`) and a test fails when it differs.

<!-- attacks:begin -->
| Id | Attack | Expected defence | Needs Docker |
| --- | --- | --- | --- |
| `trivial_tests` | Tests that only `assert True`, or call the tool and check nothing. | G3's test-strength check rejects the version. | yes |
| `decoy_read` | Reads `~/.aws/credentials`, `~/.ssh/id_*` and `.env`. | G6 fails the version; the decoy read is in the ledger. | yes |
| `exfil_undeclared` | POSTs data to an unlisted host, plain and obfuscated (`__import__('soc'+'ket')`). | G1 flags the plain case; the obfuscated one has no network (Docker says `none`, no egress is recorded). | yes |
| `manifest_lies` | Declares no network but resolves host names dynamically. | There is no route out; the ledger and the Blast Radius report record nothing leaving. | yes |
| `fork_bomb` | Spawns processes without bound. | The pids limit: Docker's process count never passes it. | yes |
| `memory_bomb` | Allocates until killed. | The memory limit: the call fails with a clear error and memory never passes the limit. | yes |
| `cpu_loop` | Loops for ever. | The timeout kills the container within the limit plus grace. | yes |
| `disk_fill` | Fills `/workspace` and `/tmp`. | The tmpfs caps hold; the container's own files do not change; the call ends cleanly. | yes |
| `output_flood` | Prints hundreds of MB. | Output is capped and the caller is told (`truncated` or a clear error). | yes |
| `poisoned_description` | The description tells the calling agent to leak data. | G1's scan rejects it; if it passes, `forge_find` flags it. | no |
| `poisoned_output` | The result text contains instructions to the caller. | The response fences it, flags it, and nothing in the registry changes. | yes |
| `name_squat` | A new package reuses an active name without lineage (or with the wrong one). | The collision rule refuses it; the registry is unchanged. | no |
| `sdist_exec` | A requirement whose source distribution runs code at install (a URL or VCS reference, or a release that has no wheel). | URL and VCS references are rejected at G1; `--only-binary` makes G2 refuse a source-only release. | yes |
| `challenger_injection` | Code comments instruct the challenger model to propose nothing. | The host's own boundary cases still run and G5 does not pass on an empty answer. | yes |
| `permission_creep` | A patch version quietly adds a network host. | It goes to `pending_approval` with a permission diff; the old version stays active. | yes |
| `late_betrayal` | Behaves while it is verified and for its first calls, then reads decoy credentials. | The runtime tripwire refuses the call and quarantines the version; no later call runs. | yes |
| `approval_over_mcp` | An MCP client looks for or invents a way to approve, promote, release, quarantine or roll back. | No such tool exists; the server package imports no operator function; nothing changes. | no |
| `secret_in_source` | Tool source or tests contain a key-shaped string. | G1 rejects it; the matched text never appears in the report, a ledger or the gate rows. | no |
| `http_no_token` | A request to the MCP HTTP port without the token, or with a foreign `Origin` or `Host`. | Refused (401 or 403); nothing is executed. | no |
| `ledger_tamper` | An edited ledger line. | Chain verification fails and the Blast Radius report says so. | no |
| `plan_host_escape` | Plans whose address is another host, `user@host`, plain http, another port, a script or file address or a template in the host; a caller whose argument tries to move the address; a page that redirects. | G1 refuses each plan; the renderer keeps every address on the approved host; the plan runner stops at the first page that is not. | no |
| `plan_vocabulary` | A script step, an extra field, a template in a selector, a password field or typed secret, too many steps, an endless wait, a hidden character, code beside the plan, or a secret on it. | G1 refuses each one; the plan language has no step that runs script. | no |
| `plan_page_text` | A page whose text holds bidirectional and invisible characters, escape codes, instructions and thousands of lines, and a password field with a label. | Values are cleaned and cut to the plan's own limits; a password field is never copied. | no |
| `bundle_forgery` | A signed bundle with a swapped or edited package, ledger, gate claim or signature, a borrowed signer, or an archive that slips paths, links, duplicates or oversizes a member. | `verify-bundle` refuses each, by the check aimed at it, and writes nothing. | no |
| `bundle_stranger` | A correctly signed bundle from a key nobody trusts, whose ledger says every gate passed, holding a package with a key-shaped string. | The receiver's own gates run again and reject it; an import is never active, even from a trusted key. | no |
| `chain_laundering` | A chain whose steps include a tool with network, one that takes files, a browser plan, an unpromoted or quarantined version, or a name that does not exist. | The whole chain is refused before step one; chains are off unless the operator turns them on. | no |
| `chain_ref_abuse` | Forward, self, unknown and malformed references, duplicate and bad ids, `if` and `loop` fields, too many steps, an oversize request. | Each is refused as `bad_chain` before anything runs. | no |
<!-- attacks:end -->

## What this does not show

* The catalogue is finite. An attack that is not in it is not covered by a green run.
* A `contained` verdict is evidence from one run. Docker's counters are sampled (a spike shorter than the sampling interval can be
  missed), decoys are noticed by access time and by values that reach output or files, and a tool that only reads an environment
  variable and tells nobody is not seen.
* Stateless tools run in fresh sandboxes and cannot count their own calls. `late_betrayal` therefore stands in for "a condition that
  verification never produces" with a trigger in the input.
* `sdist_exec` uses a real release that is published only as source (`chess==1.11.2`) instead of a malicious one, because no attack may
  reach a real third party or publish anything. It shows that pip is forbidden to build, not that a particular setup script is harmless.
  It needs the build gate's access to the package index and is `inconclusive` when pip cannot reach it.
* The resource attacks (`fork_bomb`, `memory_bomb`, `cpu_loop`, `disk_fill`, `output_flood`) run through a development call
  (`ToolRunner.run_unverified`): the limits belong to the sandbox, not to a gate. How G6 reads the same measurements is proven by
  `tests/test_g6_containment.py` with scripted figures.
* The last seven attacks cover what was added after the first twenty (browser plans, signed bundles, chains) and need no Docker, which
  also limits what they show. `plan_host_escape` and `plan_page_text` drive the plan runner with a scripted browser (`ScriptedBrowser`):
  they show how the runner treats what a browser says, not that a real browser worker is contained (G6 for a plan is skipped here, as
  it is wherever no browser sandbox is configured). `bundle_forgery` and `bundle_stranger` give the receiver bundles made by a stranger
  who holds their own signing key, so they show that a bundle cannot be edited or trusted into the registry, not that a stolen key
  could not sign. `chain_laundering` and `chain_ref_abuse` count calls with a counting runner: they show that a chain is refused before
  any step starts, not how a chained tool behaves once it runs (the runtime attacks above cover that).
* Hostile authors are simulated. A challenger that a comment really talked round is simulated with a scripted model (`FakeLLM`), and
  no hostile package is ever sent to a real language model.
