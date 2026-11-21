# Verification

Every submitted tool goes through a pipeline of gates before it can be used. This page says what each gate does, what it can and cannot
prove, how to run it, and how to read a rejection. The tool list below is generated from `toolforge.verify.pipeline.GATE_INFO`, the one
list of gates in the repository; `tests/test_verification_docs.py` fails when this page and the code disagree.

## The gates

Gates G1 to G4 and G6 are built. G5 and G7 are planned and are recorded as `skipped` for now, which caps a passing version at `verified_basic`.

<!-- gates:begin -->
| Gate | Name | Status | What it does | What it catches | Example |
| --- | --- | --- | --- | --- | --- |
| G1 | Static check | built | Reads the tool's files without running anything and looks for risky code, secret-shaped text and hidden instructions. | Network libraries without declared hosts, native-code loaders, keys and tokens left in a file, and descriptions that try to give orders to an AI. It is a quick advisory scan that a determined author can get around; the sandbox is the real boundary. | A tool that imports `socket` but declares no network hosts is rejected before anything runs. |
| G2 | Build | built | Installs the tool's declared dependencies from ready-made wheels only and freezes everything into one sealed file. | Dependencies that cannot be installed without running their setup scripts, versions that do not exist, undeclared dependencies, and packages too big for the sandbox. | A pinned library that only exists as source code has no wheel, so the build fails instead of running its setup script. |
| G3 | Self-tests | built | Runs the tool's own tests and its manifest examples in a sandbox with no network, then checks that the tests really check something. | Tests that fail, examples whose answer is wrong, and tests that would still pass if the tool returned nothing. | A suite made only of `assert True` still passes when the tool returns nothing, so it is rejected. |
| G4 | Schema fuzz | built | Feeds the tool generated inputs that are valid by its own input schema: empty text, long text, zero, big numbers, empty lists. | Crashes, hangs and oversized or non-JSON answers on inputs the schema says are allowed. Refusing with a clear message (`ToolError`) is fine; crashing is not. | A tool that crashes on an empty list fails, with the input that broke it. |
| G5 | Challenger | planned | A second AI model, not the one that wrote the tool, tries to break it with inputs the author did not think of. | Mistakes the author's own tests missed. | The challenger finds that the tool miscounts words that contain an apostrophe. |
| G6 | Containment | built | Runs the tool with the network closed and decoy secrets planted (fake keys, an SSH key, an .env file), and watches what it touches. | Reading, printing or copying the decoy credentials, going past the memory, process or CPU limits, changing the container's own files, and a sandbox that is less locked down than it should be. The network is closed for this run, so it cannot show what a tool does with network access; a clean run is evidence, not proof. | A tool that opens `/tmp/.aws/credentials` is caught by the decoy planted there. |
| G7 | Approval | planned | A human operator decides on tools that ask for network access or secrets. | Permissions nobody agreed to. | A tool that asks to call `api.example.org` waits until the operator approves exactly that host. |
<!-- gates:end -->

A gate that crashes counts as failed, with a generic message (the details go to the log, not to the report). A gate that runs past its
time budget fails, unless it had already failed with its own message. The first failure stops the run and the later gates are shown as
`not_run`. G1 is the exception to "first problem only": it lists every problem it finds at once.

| Gate | Time budget |
| --- | --- |
| G1 | 60 seconds |
| G2 | 300 seconds (the pip step itself is limited to 240) |
| G3 | 360 seconds (tests 120, examples 60, strength check 120) |
| G4 | 210 seconds (inputs 120) |
| G6 | 180 seconds (inputs 90) |

## Running it

```text
toolforge verify examples/toolpacks/word_count     # a dry run: nothing is written to the registry or the ledger
toolforge submit examples/toolpacks/word_count     # registers the version and records every gate result
toolforge verify <dir> --json                      # the same report as JSON
toolforge verify <dir> --fuzz-inputs 60            # more inputs for G4 (1 to 200, default 30)
```

Both commands exit 0 when the package passed every gate that could run (`verified_basic` or `verified`) and 1 when it was rejected.
G2 to G4 run the package's code, so both commands need Docker; the package's code never runs in the ToolForge process.

A version that could not finish because the sandbox was unavailable stays `verifying`. Submitting the same content again resumes it.
Different content under the same `name@version` is refused (`version_exists`): versions never change, raise `version` and set
`parent_version`. A rejected version keeps its number for the same reason.

## G1: what it looks for

The scan reads every `.py` file with the `ast` module and every file with the secret scanner. It is advisory: a determined author can get
around any static check, and the sandbox (G6) is the real boundary.

<!-- g1-codes:begin -->
| Code | Effect | Rule | What to do |
| --- | --- | --- | --- |
| `network_import` | rejects | The code imports a networking library (`socket`, `ssl`, `http`, `urllib.request`, `urllib3`, `requests`, `httpx`, `aiohttp`, `ftplib`, `smtplib`, `telnetlib`, `websockets`) while `permissions.network` is empty. | declare the host in permissions.network, or remove the import: a tool without declared hosts has no network |
| `native_code` | rejects | The code imports `ctypes` or `pty`. | remove it: tools run as pure Python, and native calls or pseudo-terminals are not allowed |
| `secret` | rejects | A string in any file looks like a key or token (see the pattern list below). The matched text is never shown. | remove it and read secrets through a name declared in permissions.secrets; use obviously fake values in tests |
| `injection_text` | rejects | The description, `when_to_use` or example text looks like instructions aimed at an AI. | rewrite it as plain text that says what the tool does and when to use it; leave out orders and role changes |
| `dynamic_code` | warns | `eval`, `exec` or `compile` is called, or `__import__` / `importlib.import_module` gets a non-literal name. | avoid running text as code; if you need it, say why in the description |
| `process_spawn` | warns | The code imports `subprocess` or calls `os.system` or `os.popen`. | prefer library calls; starting programs is limited by the sandbox and may fail there |
| `environment_read` | warns | The code reads `os.environ` or calls `os.getenv`. | tools get no secrets in the environment; declare a secret in permissions.secrets instead |
| `sensitive_path` | warns | A string names a path under `/etc`, `/proc`, `/root`, `~/.ssh` or `~/.aws`. | tools work in /workspace; they have no reason to read system or credential folders |
<!-- g1-codes:end -->

### Secret patterns

One list of named patterns, in `toolforge/verify/secretscan.py`. The report names the pattern, the file and the line, never the matched
text. The same scan is meant to be the last check before any text is sent to a model provider (P10).

<!-- secret-patterns:begin -->
| Pattern | What it looks for |
| --- | --- |
| `aws_access_key_id` | An AWS access key id: `AKIA` followed by 16 upper-case letters or digits. |
| `github_token` | A GitHub token with a `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` prefix. |
| `github_fine_grained_token` | A GitHub fine-grained token: `github_pat_` followed by at least 22 letters, digits or underscores. |
| `slack_token` | A Slack token: `xoxb-`, `xoxa-`, `xoxp-`, `xoxr-` or `xoxs-` followed by at least 10 characters. |
| `google_api_key` | A Google API key: `AIza` followed by 35 letters, digits, `_` or `-`. |
| `private_key_header` | The first line of a PEM private key (`-----BEGIN ... PRIVATE KEY-----`). |
| `secret_assignment` | A name containing `key`, `token`, `secret` or `password` assigned a quoted value of 20 or more mixed, random-looking characters. Hashes, UUIDs and placeholders are not flagged. |
<!-- secret-patterns:end -->

## G2: building

* No `requirements`: nothing is installed and the frozen package is the package itself, so its `blob_sha` equals its `code_sha`.
* With `requirements`: one sandbox with network access limited to `build_allow` runs
  `pip install --only-binary=:all: --no-deps --no-compile --no-cache-dir --target /workspace/pkg/.deps -r /workspace/requirements.txt`.
  Wheels only, so no `setup.py` runs at install time; `--no-deps`, so list every dependency yourself; `--no-compile` and
  `--no-cache-dir` keep the frozen package small and the same every time.
* The result is stored as the version's blob. A resumed run reuses the blob it already has.

## G3: self-tests

The tests run through ToolForge's own small runner inside the sandbox, never through pytest and never through anything the author wrote.

What the runner supports: top-level `def test_*` functions and `unittest.TestCase` methods in `tests/test_*.py`, plain `assert`, helper
modules imported from `tests/`, and `import tool`. What it does not: fixtures, `parametrize`, `conftest.py`, async tests, plugins. Every
test runs in its own process with a 10 second limit; tests share the workspace's files but never memory.

Then every manifest example that gives an `output` runs and must return it (as JSON, numbers within 1e-9, `True` is not `1`). Examples
marked `live` are skipped.

Finally the **strength check** runs the tests again with the tool's function replaced by one that returns nothing. If every test still
passes, your tests do not constrain behaviour and the gate fails: assert on returned values. This proves the tests are not empty; it
does not prove they are good.

## G4: schema fuzz

Inputs are generated from the package's `input_schema` and examples, seeded by `code_sha`, so the same package always gets the same
inputs: empty and very long strings, unicode, zero, negative and large numbers, empty and full lists, every enum member, objects with
only the required fields, and the manifest examples with one property changed at a time.

* Returning a result or raising `ToolError` (a handled refusal) is fine.
* A crash, a timeout, an oversized result or a result that is not JSON fails the gate, and the hint names the input that did it.
* If every input was refused, the gate passes with a warning that the main path was not exercised.

## G6: containment

One sandbox is opened with the **network closed** and MiniLocker's decoy credentials planted: fake cloud keys, an SSH key, an `.env`
file and variables in the environment. The tool then runs on every manifest example that is not `live` and on the first 10 generated
inputs of G4 (same seed, so the same inputs), one process per input. Before the sandbox is destroyed, MiniLocker audits it and the gate
reads what the audit found. It **fails** when:

* a decoy was read, shown in output, copied into a file or used. No tool has a reason to look for credentials;
* the sandbox was not locked down as it should be (a runner problem, but a tool must never be verified in a weaker box than the one it
  runs in);
* the container's own files changed, a host folder was mounted, or a real secret was found inside. The sandbox's own tmpfs mount points
  (`/workspace`, `/tmp`) are not counted: Docker lists them as modified even when nothing was written;
* memory, process count or CPU went past the limits, or an input hit the time limit.

It is **skipped, never passed,** when the sandbox could not observe what it needs to: no decoys were planted, the hardening could not
be read, the audit was incomplete, or there was nothing to run. A skipped gate keeps the version at `verified_basic`. On a machine
without Docker the pipeline stops before any gate can use a sandbox, so G6 never reports a pass it did not earn.

What G6 does not show: the network is closed during this run, so a tool cannot try to connect and **egress is "not applicable"**, never
"clean". What a tool does with a network it was approved for is a matter for approvals (P09). A tool that reads an environment variable
and tells nobody is not seen. A passing G6 is evidence from one run, not proof.

### After verification: the runtime tripwire

Real calls are run with the same decoys (`RUNTIME_DECOYS`, on by default). If a tool touches one during a call:

* the call fails with `PolicyViolation`; its result, stdout and stderr are discarded (a decoy value could be in them);
* the **active version is quarantined** by actor `forge`, so it can no longer be found or run;
* `forge.quarantined` is written to `ledgers/<tool>@<version>/operator.jsonl`, with the call id and the decoy **labels** (never a value).

Only a person can undo it, with `toolforge release`. Limit to know about: a caller who passes a decoy path (for example
`/tmp/.aws/credentials`) to a tool that opens caller-chosen paths can trigger a quarantine, which is a denial of service. It is
reversible by `release`, and the SDK confines file arguments to `/workspace/inputs`.

## Blast Radius

The Blast Radius of a version says what it was seen doing while it was watched, measured from the hash-chained ledgers and never
claimed. It is stored as `ledgers/<tool>@<version>/blast-radius.json` next to them, built when verification finishes (and again by
`toolforge report`). It has five dimensions, each with a status: `network`, `filesystem`, `secrets`, `compute` and `persistence`.
MiniLocker's policy, time, browser and artifact dimensions describe an agent's shell and browser, so they are listed as
`not_applicable` instead of being shown as zeros.

* **Scope.** Only the containment run counts (from the start of G6 on). The build gate's package downloads cannot show up as network.
  The whole ledger is still checked for tampering.
* **A gap is not a zero.** A dimension that nothing observed says `not measured`. `network` says `no_route` only when Docker itself
  reported no network for every sandbox.
* **No G6, no measurement.** A version whose verification ledger holds no containment run (verified before G6 existed, or stopped
  before it) is reported as `not_measured`; the build and test sandboxes are never read in its place.
* **Finished means destroyed.** A run is finished when every sandbox it created has a matching `sandbox.destroyed` event.
* **A sample of real calls** (the newest 20 call ledgers) is added as `runtime_sample`.

The verdict is one of `ledger_broken` (a ledger did not verify, so nothing can be trusted), `violation_seen`, `not_measured`,
`incomplete` and `no_violation_seen`. The last one is evidence, not proof. A report that cannot be built is a warning on the
verification, never a failure of it.

| Command | What it does |
| --- | --- |
| `toolforge report <name@version> [--json \| --html]` | Build the report again from the ledgers, store it and print it. Needs no Docker. |
| `toolforge quarantine <name@version> --reason "..."` | Operator: take an active version out of use. |
| `toolforge release <name@version> --reason "..."` | Operator: print why it was quarantined and its Blast Radius, then make it active again. |

`release` prints the recorded quarantine reason and the report first and then acts; it does not ask for confirmation, the required
`--reason` is the decision. If another version of the tool is active, it is deprecated, as in `promote`. `forge_describe` shows the
stored report under `blast_radius` (a missing one is shown as `not_measured`), with a note that paths and names in it came from
running the tool. No MCP tool can release or quarantine.

## What the report holds

A report lists each gate with its status, a one-line summary, short hints and a few numbers. Text that came from a sandbox is clipped and
cleaned before it is shown. The ledger for a run (`ledgers/<tool>@<version>/verify-<run id>.jsonl`) holds hashes, counts and labels:
`forge.submitted`, `forge.verifying`, `forge.gate.started`, `forge.gate.passed`, `forge.gate.failed`, `forge.gate.skipped`,
`forge.verification.aborted`, then `forge.verified` or `forge.rejected`. Source text never enters it.

## Not measured

Gate G6 has run only against the tools in this repository and, once `pytest -m docker` has been run, the Docker tests; it has not been
measured against hostile tools yet: that is the attack lab (P15). G5 (a second model) arrives in P10 and the operator's approval (G7) in
P09. Network behaviour is not measured at all in this release (see G6).
