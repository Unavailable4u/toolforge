# P06: Verification pipeline: gates G1 to G4

Depends on: P03, P05.

## Goal

Turn "an agent submitted a package" into a verdict. Build the pipeline framework and the first four gates. Gates G5 to G7
arrive later and plug into the same interface.

## Scope

`src/toolforge/verify/{pipeline.py,g1_static.py,g2_build.py,g3_selftest.py,g4_fuzz.py,schemagen.py,secretscan.py}`,
`src/toolforge/core/forge.py` (`Forge.submit`), harness extensions (`--tests`, `--batch`, `--stub`), CLI
`toolforge verify <dir>` (dry run, no registry writes) and `toolforge submit <dir>`, tests.

## Framework

```python
class Gate(Protocol):
    id: str
    def run(self, ctx: GateContext) -> GateResult
GateResult(status: "passed"|"failed"|"skipped", summary: str, hints: list[str], metrics: dict, detail: dict)
```

`GateContext` carries the package, settings, runner, ledger, the blob produced by G2 (set by G2), the registry handle and a
deadline. `Pipeline.run(package, submitted_by)` runs gates in order, writes `forge.gate.started/passed/failed/skipped`
events, persists `gate_results`, stops at the first failure (G1 reports all of its issues at once), and moves the version
through the lifecycle: `submitted -> verifying -> rejected` or `verified_basic` (while G5 to G7 are not built, skipped gates
cap the status at `verified_basic`). Each gate has a wall-clock budget; exceeding it is a failure with a hint.

**`GATE_INFO`** (in `verify/pipeline.py`) is an ordered tuple describing all seven gates:
`GateInfo(id, name, plain_summary, catches, example, status)`, where `plain_summary`, `catches` and `example` are short
plain-language sentences (no jargon without explanation) and `status` is `built` or `planned`, derived from which gate classes are
registered. It is the only list of gates in the repository. Docs, the README, drift tests and the website (P19) read it. Gates
G5 to G7 are described here already, with status `planned`, so the list is complete from the first release of this patch.

`Forge.submit(payload, submitted_by)`: parse; validate (P02); reject immediately if `name@version` exists with a different
`code_sha` ("versions are immutable: bump `version`"); return the existing report if the same content is resubmitted;
otherwise register `submitted` and run the pipeline. Returns a `VerificationReport` (status, version, gates, `next_action`).

## G1: static

Everything from P02 validation plus `ast` analysis of every `.py` file:

* Imports of network libraries (`socket, ssl, http, urllib.request, urllib3, requests, httpx, aiohttp, ftplib, smtplib, telnetlib, websockets`) while `permissions.network` is empty: fail.
* `eval`, `exec`, `compile`, `__import__`, `importlib.import_module` with non-literal arguments, `ctypes`, `os.system`,
  `subprocess`, `os.popen`, `pty`: recorded as findings; **fail** by default for `ctypes/pty`, **warn** for the rest (warnings
  are shown to the operator and stored; they do not block). A setting may later make warns fatal.
* Reads of `os.environ`, paths under `/etc`, `/proc`, `/root`, `~/.ssh`, `~/.aws`: warn.
* **Secret-shaped strings** in any file (source, tests, manifest, examples, sample data): **fail**. `verify/secretscan.py` holds one
  constant list of named patterns and a function `scan(text) -> list[Finding(pattern, file, line)]`. Patterns include AWS access
  key ids (`AKIA` plus 16 characters), GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), Slack tokens
  (`xox[baprs]-`), Google API keys (`AIza` plus 35 characters), PEM private-key headers, and assignments such as
  `key|token|secret|password = "<20 or more high-entropy characters>"`. One test per pattern, plus tests that ordinary hashes,
  UUIDs and base64 test data are not flagged. The matched text is **never** echoed in the report, a hint, the ledger or a log: report the
  pattern name, file and line only. The hint says to read secrets through a brokered name declared in `permissions.secrets` (P09).
  The same `scan` is reused by the LLM client (P10) as a last check before anything is sent to a provider.
* Description, `when_to_use` and example text scanned with `textsafety.scan`; a hit fails with a hint to rewrite plain text.
* Static analysis is advisory (it is trivially bypassed). Say so in the gate summary; G6 is the real check.

## G2: build

* Package has no `requirements`: skip installing, thaw nothing, create the blob directly from the package (`to_tar`), still
  record `blob_sha`.
* Otherwise open a sandbox with `network = tuple(settings.build_allow)` and run
  `python -m pip install --no-input --disable-pip-version-check --no-deps --only-binary=:all: --target /workspace/pkg/.deps -r requirements.txt`
  (the host writes `requirements.txt` from the manifest). `--only-binary` means no source distribution and therefore no
  `setup.py` execution at install time. Check the workspace stays under its cap, `freeze()`, store the blob, record
  `blob_sha`, the pip output tail (sanitised) and the installed file count.
* Failure hints: "pip could not find a wheel for X==Y; pick a version with a wheel for CPython 3.12 manylinux", "list every
  dependency, installs use --no-deps", "the workspace is 64 MB; prefer libraries already in the runtime image (see forge_runtime_info)".

## G3: self-tests

* Runs in a fresh sandbox with `network="none"`, the thawed blob and the in-sandbox test runner (the harness `--tests` mode;
  **no pytest dependency**): discovers `tests/test_*.py`, runs top-level `test_*` functions and `unittest.TestCase`
  methods, reports `{passed, failed: [{id, error}], errors}` as JSON. Document the limits: no fixtures, no parametrize,
  plain `assert`.
* Also runs every manifest example (input to output, JSON equality with numeric tolerance 1e-9) when `output` is present.
* **Test-strength check.** Run the tests again with `--stub`, which replaces the entrypoint with a function that returns
  `None`. If every test still passes, fail with: "your tests do not constrain behaviour; assert on the returned values". This
  stops `assert True` tests.
* Failure hints quote the first failing test id and error (clipped, sanitised).

## G4: schema fuzz

`schemagen.py` generates deterministic inputs (seeded by `code_sha`) from `input_schema`: for each type the boundary values
(empty string, long string up to a cap, unicode, 0, -1, large ints, floats, NaN excluded, empty array, array of max items
within caps, nested objects, every enum member, required-only objects, all-optional objects) and the manifest examples
mutated one property at a time (30 inputs by default, configurable). The harness `--batch` mode runs them in one sandbox
sequentially with a per-input timeout. Pass criteria: every run returns an envelope; `Crash` is not allowed (only
`ToolError` as a handled failure); no `OutputTooLarge`/`NotJson`; total time within budget. Report the first few failing
inputs (clipped) as hints. Record metrics: inputs run, handled errors, p95 duration.

## Ledger

One ledger file per verification run under `ledgers/<tool>@<version>/`. Events: `forge.submitted` (with `code_sha`,
`submitted_by`, manifest permission summary), `forge.gate.*`, `forge.verified`. Payloads contain hashes, counts and
labels, never full source.

## Tests

Gate-by-gate with `FakeRunner` scripting the harness outputs, plus pure unit tests for `schemagen` (determinism, bounds,
every generated input validates against the schema). A property-style test that G1 never raises on random garbage packages.
Secret scan: each pattern is detected, near misses are not, and the matched text never appears in the report or ledger. `GATE_INFO`
lists G1 to G7 in order with the right `status`. Docker tests (`docker` marker): both reference packs pass G1 to G4; a package
with `assert True` tests fails G3; a package that crashes on an empty list fails G4; a package with a pinned requirement that has
only an sdist fails G2.

## Acceptance

`pytest -q`; with Docker `toolforge submit examples/toolpacks/csv_summary` ends `verified_basic` with G5 and G6 `skipped`.

## Pitfalls

Never run pytest or any author-supplied runner inside the sandbox. Never let a gate mutate the package. Keep hints short,
actionable, and free of raw untrusted text longer than 200 characters. Never print a matched secret.
