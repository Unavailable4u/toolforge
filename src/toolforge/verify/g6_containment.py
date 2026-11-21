"""Gate G6, containment: run the tool where nothing is allowed, with bait planted, and see whether it reaches for anything.

One sandbox is opened with the network closed and MiniLocker's decoy credentials planted (fake cloud keys, an SSH key, an `.env`
file, variables in the environment). The tool then runs on every manifest example that is not `live` and on the first generated
inputs from G4 (`verify.schemagen`, same seed, so the same inputs), one process per input, through the harness `--batch` mode.
Before the sandbox is destroyed MiniLocker's `Watch` audits it, and this gate reads what that audit found. It fails when:

* a decoy was read, shown in output, copied into a file or used (`canary.*` events, one definition shared with the runtime tripwire:
  `sandbox.base.decoy_violations`), because no tool has a reason to look for credentials;
* the sandbox was not locked down as it should be (`hardening` differs from the invariants below: a runner bug, not the tool's fault,
  but a tool must never be verified inside a weaker box than the one it will run in);
* the container's own files changed (`rootfs_changes`), a host folder was mounted, or a real secret was found inside;
* memory, process count or CPU went past what the manifest and the sandbox allow, or an input hit the time limit.

It is skipped, never passed, when the sandbox could not observe what it needs to (no decoys were planted, the hardening could not be
read, the audit was incomplete, there was nothing to run). A skipped gate keeps the version at `verified_basic`.

Not measured here, and said so in the report: egress. The network is closed for this run, so a tool cannot attempt a connection and
no egress is recorded; that is `not applicable`, not `clean`. What a tool does with the network it was approved for is measured at run
time (P09). Also not measured: whether a tool reads an environment variable and tells nobody; decoys are noticed by access time and by
values that reach output or files. A passing G6 is evidence from one run, not proof.

The Blast Radius report (`report.blast_radius`) is built from this run's ledger after the pipeline has finished.
"""

from __future__ import annotations

from typing import Any

from toolforge import schemacheck
from toolforge.sandbox.base import AuditSummary, decoy_violations, decoys_planted
from toolforge.verify import g4_fuzz, sandboxrun, schemagen
from toolforge.verify.pipeline import GateContext, GateResult, clip, failed, passed, skipped

BUDGET_S = 90
GENERATED_INPUTS = 10  # the first inputs of the same sequence G4 runs
MEGABYTE = 1024 * 1024
MAX_NAMED = 5
LABELS_CHARS = 60

# What the sandbox must report about itself (`MiniLockerHandle.hardening`). A key that is missing means "unknown", never "fine".
EXPECTED_USER = "65534:65534"


def _is_list(value: Any) -> bool:
    return isinstance(value, list)


def check_hardening(hardening: dict[str, Any]) -> tuple[list[str], list[str]]:
    """`(wrong, unknown)`: what the sandbox reported that breaks an invariant, and what it did not report at all."""
    wrong: list[str] = []
    unknown: list[str] = []

    def need(key: str) -> Any:
        value = hardening.get(key)
        if value is None:
            unknown.append(key)
        return value

    user = need("user")
    if user is not None and user != EXPECTED_USER:
        wrong.append("the process does not run as the unprivileged user")
    read_only = need("read_only_rootfs")
    if read_only is not None and read_only is not True:
        wrong.append("the root filesystem is writable")
    cap_drop = need("cap_drop")
    if cap_drop is not None and not (_is_list(cap_drop) and "ALL" in cap_drop):
        wrong.append("Linux capabilities were not all dropped")
    cap_add = hardening.get("cap_add")  # absent and empty both mean none were added
    if cap_add:
        wrong.append("a Linux capability was added")
    privileged = need("privileged")
    if privileged is not None and privileged is not False:
        wrong.append("the container is privileged")
    options = need("security_opt")
    if options is not None and not (
        _is_list(options)
        and any(
            isinstance(o, str) and o.startswith("no-new-privileges") and not o.endswith(":false")
            for o in options
        )
    ):
        wrong.append("no-new-privileges is not set")
    mode = need("network_mode")
    if mode is not None and mode != "none":
        wrong.append(f"the network mode is {clip(mode, 30)!r}, not 'none'")
    binds = hardening.get("binds")
    if binds:
        wrong.append("a host folder is bound into the container")
    if hardening.get("pid_mode"):
        wrong.append("the container shares the host's process namespace")
    if hardening.get("ipc_mode") == "host":
        wrong.append("the container shares the host's IPC namespace")
    return wrong, unknown


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class ContainmentGate:
    id = "G6"
    budget_s = BUDGET_S + 90

    def run(self, ctx: GateContext) -> GateResult:
        if ctx.blob is None:
            return failed(
                "there is no built package to test",
                ["G2 must pass first; this is a ToolForge problem if you see it"],
            )
        manifest = ctx.package.manifest
        examples = [e for e in manifest.examples if not e.live]
        items: list[dict[str, Any]] = [{"args": e.input, "files": e.files} for e in examples]
        try:
            generated = schemagen.generate(
                manifest.input_schema,
                manifest.examples,
                seed=ctx.package.code_sha,
                count=g4_fuzz.DEFAULT_INPUTS,
            )[:GENERATED_INPUTS]
        except schemacheck.SchemaCheckError as exc:
            return failed(
                "the input schema could not be checked in time while generating inputs",
                [f"simplify the input schema ({clip(str(exc), 100)})"],
            )
        items += [{"args": g.args, "files": g.files} for g in generated]
        if not items:
            return skipped(
                "there was no input to run (no example that is not live, and none could be generated), so nothing was watched",
                ["add a manifest example that does not need the network"],
                metrics={"inputs_run": 0},
            )
        limits = sandboxrun.limits_of(ctx)
        request = {
            "entrypoint": manifest.entrypoint,
            "limits": limits,
            "items": items,
            "include_results": False,
            "deadline_s": BUDGET_S,
        }
        with sandboxrun.open_sandbox(ctx, "G6", network="none", decoys=True) as handle:
            sandboxrun.load_package(handle, ctx.blob)
            run = sandboxrun.run_harness(
                handle,
                ["--batch"],
                request,
                timeout_s=BUDGET_S + sandboxrun.GRACE_S,
                max_out=len(items) * 4096,
            )
            summary = (
                handle.close()
            )  # the audit happens here; closing again on the way out returns the same summary
        return self._judge(run, summary, len(items), len(examples), limits)

    # -- reading what the audit says ---------------------------------------------------------------------------------------

    def _judge(
        self,
        run: sandboxrun.HarnessRun,
        summary: AuditSummary,
        planned: int,
        examples: int,
        limits: dict[str, int],
    ) -> GateResult:
        problems: list[str] = []
        hints: list[str] = []
        unobserved: list[str] = []
        metrics: dict[str, Any] = {
            "inputs_planned": planned,
            "examples": examples,
            "egress": "not applicable (no network)",
        }
        detail: dict[str, Any] = {}

        # 1. Decoys: the main purpose of this gate.
        planted = decoys_planted(summary.decoy_events)
        touched = decoy_violations(summary.decoy_events)
        names = sorted(
            {
                clip(
                    (e.get("payload") or {}).get("label") or (e.get("payload") or {}).get("item") or "?",
                    LABELS_CHARS,
                )
                for e in touched
            }
        )
        metrics["decoys_planted"] = planted
        metrics["decoy_events"] = len(touched)
        if touched:
            ways = sorted({str(e.get("type", "")).removeprefix("canary.") for e in touched})
            problems.append(
                f"the tool touched {len(names)} planted decoy credential(s) ({', '.join(names[:MAX_NAMED])}): {', '.join(ways)}"
            )
            hints.append(
                "a tool has no reason to look for credentials, keys or environment files; remove any code that searches the "
                "home folder, /tmp or the environment, and do not print or save environment variables"
            )
            detail["decoys_touched"] = [
                {
                    "item": clip((e.get("payload") or {}).get("item"), 40),
                    "how": str(e.get("type", "")).removeprefix("canary.")[:20],
                }
                for e in touched[:20]
            ]
        elif planted == 0:
            unobserved.append("no decoy was planted, so nothing could be seen being touched")

        # 2. The sandbox must be as locked down as it claims.
        wrong, unknown = check_hardening(summary.hardening) if summary.hardening else ([], ["everything"])
        if wrong:
            problems.append("the sandbox was not locked down: " + "; ".join(wrong[:MAX_NAMED]))
            hints.append(
                "this is a ToolForge or Docker configuration problem, not a problem in the package; run `toolforge doctor` "
                "and ask the operator to look at the log"
            )
        elif unknown:
            unobserved.append(
                "the sandbox did not report its hardening (" + ", ".join(unknown[:MAX_NAMED]) + ")"
            )
        metrics["hardening_confirmed"] = bool(summary.hardening) and not wrong and not unknown

        # 3. Anything recorded that cannot be: egress with the network closed.
        if summary.egress_attempts:
            problems.append("the ledger records network traffic from a sandbox that had no network")
            hints.append(
                "this is a ToolForge problem, not a problem in the package; ask the operator to look at the log"
            )
            metrics["egress"] = len(summary.egress_attempts)

        # 4. The audit: files, mounts, secrets, resources.
        measured = summary.measured if isinstance(summary.measured, dict) else {}
        incomplete = (
            [clip(x, 40) for x in (measured.get("incomplete") or []) if isinstance(x, str)]
            if measured
            else []
        )
        if not measured:
            unobserved.append("the sandbox gave no audit")
        elif incomplete:
            unobserved.append("the audit was incomplete (" + ", ".join(incomplete[:MAX_NAMED]) + ")")
        fs: dict[str, Any] = measured["fs"] if isinstance(measured.get("fs"), dict) else {}
        secrets: dict[str, Any] = measured["secrets"] if isinstance(measured.get("secrets"), dict) else {}
        stats: dict[str, Any] = measured["stats"] if isinstance(measured.get("stats"), dict) else {}
        changes = sandboxrun.count(fs.get("rootfs_changes"))
        binds = fs.get("host_binds") if isinstance(fs.get("host_binds"), list) else []
        found = secrets.get("found") if isinstance(secrets.get("found"), list) else []
        if measured and fs:
            metrics["rootfs_changes"] = changes
            if changes:
                changed = [
                    clip(c.get("path"), 80)
                    for c in (fs.get("rootfs_changed") or [])[:MAX_NAMED]
                    if isinstance(c, dict)
                ]
                problems.append(
                    f"the container's own files changed ({changes} changes, for example {', '.join(changed) or 'unknown'})"
                )
                hints.append(
                    "write only below /workspace or /tmp; the rest of the file system is read-only for a reason"
                )
            if binds:
                problems.append("a host folder was mounted into the sandbox")
                hints.append(
                    "this is a ToolForge problem, not a problem in the package; ask the operator to look at the log"
                )
            if fs.get("inside") is not True:
                unobserved.append("the files inside the sandbox could not be scanned")
        elif measured:
            unobserved.append("the file system was not audited")
        if found:
            problems.append("a real secret was found inside the sandbox")
            hints.append(
                "this is a ToolForge problem, not a problem in the package; ask the operator to look at the log"
            )

        if stats:
            self._resources(stats, run, planned, limits, metrics, problems, hints)
        elif measured:
            unobserved.append("memory, process count and CPU were not measured")

        # 5. The run itself.
        if not run.usable:
            problems.append(sandboxrun.describe_failed_run(run, "the containment run"))
            hints.append(
                "the tool must return from every call and stay inside its memory limit; do not call os._exit or hang"
            )
        else:
            self._items(run, planned, metrics, problems, hints)

        detail["observed"] = {
            "decoys_planted": planted,
            "audit_incomplete": incomplete[:10],
            "hardening_reported": bool(summary.hardening),
        }
        if problems:
            return failed(
                f"{len(problems)} containment problem(s): {problems[0]}",
                hints,
                metrics=metrics,
                detail=detail,
            )
        if unobserved:
            return skipped(
                "containment could not be confirmed: " + "; ".join(unobserved[:MAX_NAMED]),
                [
                    "nothing was found, but G6 only passes when the sandbox could be watched; the version stays at verified_basic"
                ],
                metrics=metrics,
                detail={**detail, "warnings": ["G6: " + u for u in unobserved[:MAX_NAMED]]},
            )
        return passed(
            f"no decoy was touched in {metrics.get('inputs_run', planned)} runs; the sandbox was as locked down as required "
            f"and stayed inside its limits (egress is not measured: the network was closed)",
            metrics=metrics,
            detail=detail,
        )

    @staticmethod
    def _resources(
        stats: dict[str, Any],
        run: sandboxrun.HarnessRun,
        planned: int,
        limits: dict[str, int],
        metrics: dict[str, Any],
        problems: list[str],
        hints: list[str],
    ) -> None:
        memory = _number(stats.get("peak_memory_bytes"))
        pids = _number(stats.get("peak_pids"))
        pids_cap = _number(stats.get("pids_limit"))
        cpu = _number(stats.get("cpu_seconds"))
        limit_mb = limits["memory_mb"]
        if memory is not None:
            metrics["peak_memory_mb"] = round(memory / MEGABYTE, 1)
        if pids is not None:
            metrics["peak_pids"] = int(pids)
        if cpu is not None:
            metrics["cpu_seconds"] = cpu
        if stats.get("oom_killed") is True:
            problems.append(f"the sandbox ran out of memory (limit {limit_mb} MB)")
            hints.append(f"use less memory, or declare a higher limits.memory_mb than {limit_mb}")
        elif memory is not None and memory > limit_mb * MEGABYTE:
            problems.append(f"memory use was {memory / MEGABYTE:.0f} MB; the limit is {limit_mb} MB")
            hints.append(f"use less memory, or declare a higher limits.memory_mb than {limit_mb}")
        if pids is not None and pids_cap and pids >= pids_cap:
            problems.append(
                f"the tool started {int(pids)} processes and reached the limit of {int(pids_cap)}"
            )
            hints.append("do not start many processes or threads; a tool needs one")
        allowed_cpu = planned * limits["timeout_s"] + sandboxrun.GRACE_S
        if cpu is not None and cpu > allowed_cpu:
            problems.append(f"CPU time was {cpu:.0f} seconds; the limits allow {allowed_cpu}")
            hints.append("make the tool do less work per call")

    @staticmethod
    def _items(
        run: sandboxrun.HarnessRun,
        planned: int,
        metrics: dict[str, Any],
        problems: list[str],
        hints: list[str],
    ) -> None:
        read = sandboxrun.read_batch_report(run.report, planned)
        if read is None:
            problems.append("the containment run's report did not account for every input")
            hints.append("the tool must return from every call; do not call os._exit or hang")
            return
        outcomes, not_run = read
        timeouts = sum(1 for o in outcomes if o.error_type == "Timeout")
        metrics["inputs_run"] = len(outcomes)
        metrics["crashes"] = sum(
            1 for o in outcomes if o.error_type == "Crash"
        )  # G4 judges crashes; recorded here for the report
        metrics["timeouts"] = timeouts
        if timeouts:
            problems.append(f"{timeouts} input(s) reached the time limit")
            hints.append("make the tool faster, or declare a higher limits.timeout_s")
        if not_run:
            problems.append(
                f"only {len(outcomes)} of {planned} inputs ran before the {BUDGET_S} second budget"
            )
            hints.append("make the tool faster")


__all__ = ["BUDGET_S", "ContainmentGate", "check_hardening"]
