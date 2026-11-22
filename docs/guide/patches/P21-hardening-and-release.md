# P21: Hardening pass and release

Depends on: all earlier patches (P01 to P20). S01 and S02 are optional; if either was built, it is included in the checks below.

## Goal

Close the gap between "works" and "trustworthy": a threat model that matches the code, CI that exercises the real stack,
drift tests that stop documents from lying, and a tagged release.

## Scope

`SECURITY.md`, `CHANGELOG.md`, CI expansion, additional drift tests, error-message audit, dependency audit, performance
notes, registry garbage collection, version bump to `0.1.0`.

## Work items

1. **Threat model** (`SECURITY.md`): assets (host, credentials, registry integrity, the calling agent's context), adversaries
   (malicious or confused tool author, hostile content the agent read, compromised challenger or builder model, a curious
   tool at runtime, another process or network peer reaching the MCP HTTP port or the dashboard), guarantees (what the
   container boundary and gates provide), explicit non-guarantees (static checks are advisory; regex scanners are
   bypassable; the LLM challenger can be wrong or fooled; the global egress allowlist; timing and side channels not
   addressed), and a reporting address. Every claim points at the test that proves it.
2. **Drift tests**: README tool table equals real MCP tools (`META_TOOLS`); every setting documented; attack catalogue equals
   `docs/ATTACKS.md`; benchmark task list equals `docs/BENCHMARK.md`; recipes validate; `doctor` checks match the docs; the
   site's tool table, gate cards and attack table equal their source constants (P19); the README quick start equals
   `site.json:quickstart`.
3. **CI**: two jobs. Fast: lint plus unit tests plus the site build and its offline checks. Full: build the runtime image,
   run `pytest -m docker`, the golden packs and `pytest -m attacks` on a runner with Docker, and a smoke run of the compose
   stack. Cache nothing that could hide a dependency problem.
4. **Error-message audit**: grep every raised `ToolForgeError` and every MCP-facing message; each must say what failed, why and
   what to do, with no host paths, no Docker identifiers, no raw untrusted text over 200 characters. Add tests for the worst offenders.
5. **Dependency audit**: exact pins, license compatibility, no dependency that is unused; document how to update the MiniLocker pin.
   Check that no CI action is referenced by a floating tag.
6. **Resource hygiene**: confirm no test, attack or demo leaves containers, networks or temp dirs behind (`doctor` clean after a
   full run); verify reaper behavior after a simulated crash.
7. **Registry garbage collection**: `toolforge registry gc [--dry-run] [--older-than DAYS]` removes blobs (never rows, never
   ledgers) belonging to versions that are `rejected` and older than the threshold, and blobs referenced by no version. Rows keep
   `code_sha`, so history stays intact. Test with an injected clock; `--dry-run` is the default.
8. **Performance notes** (`docs/PERFORMANCE.md`): measured cold-start per call, per gate durations for the reference packs, memory.
   If cold start dominates, document a warm-pool design (reuse an idle sandbox only for the same tool version, never across
   callers) as future work; implement only if the numbers justify it and the isolation story stays intact.
9. **Site and docs final check**: rebuild `results.json` from fresh runs, rebuild the site with `--release`, confirm the Pages
   deployment URL loads, every number on the site matches the newest results files, and no section claims something whose
   chip is `planned`. Record the check in the release notes.
10. **Release**: `CHANGELOG.md`, version `0.1.0`, tag instructions in the notes, `docs/guide/` kept as "how this repository was built".

## Tests

Work items 2, 4, 6 and 7 are tests, not prose: the full drift suite; an audit test that fails on an error message containing a host path
or a Docker identifier; a hygiene test that no containers, networks or temp dirs remain after the docker and attack suites; the garbage
collection test with an injected clock. CI itself (item 3) is verified by one green run of each job on a clean clone, recorded in the
release notes.

## Acceptance

All CI jobs green on a clean machine; `toolforge doctor` clean; `toolforge attacks run` all `contained`; `python -m bench`
produces a report; the README quick start works from a fresh clone; the site builds and deploys.

## Pitfalls

Do not soften a limitation to make a document look better. If a test is flaky, fix the cause (or mark it with a documented
reason), do not retry it into passing.
