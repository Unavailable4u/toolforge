"""Turns raw run records (and an optional context measurement) into `summary.json` and `report.md`.

`summary.json` is what the dashboard and the website read. Its rules: a figure that was not measured is `null`, never 0; every success
rate comes with its run count and Wilson 95 percent interval; runs that could not be measured are counted under `not_measured`, apart
from the runs that count; and nothing is dropped. `report.md` is the same numbers as tables plus a plain "what this shows and does not
show" section. A difference between two configurations is called supported only when their intervals do not overlap.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from bench import stats
from bench import tasks as task_set
from bench.agent import prompt_hash

SUMMARY_SCHEMA = 1
COMPARISONS = (("baseline", "forge_cold"), ("baseline", "forge_warm"), ("forge_cold", "forge_warm"))
ATTACK_LAB_MISSING = "no Attack Lab result was given (`--attacks runs/attacks/attacks-<time>.json`)"
ATTACK_LAB_RULE = (
    "contained / (contained + breached + inconclusive): an attack the lab could not prove contained counts against the rate; "
    "skipped attacks are counted apart"
)
VERDICTS = ("contained", "breached", "inconclusive", "skipped")
OPERATOR_NOTE = (
    "forge runs use a scripted operator: it promotes verified versions that ask for no network or secrets and approves only "
    "the task's fixture host (docs/BENCHMARK.md)"
)


def _medians(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counted = [r for r in records if r.get("outcome") in ("success", "failure")]
    agents = [r.get("agent") or {} for r in counted]
    return {
        "median_tool_calls": stats.median(a.get("steps") for a in agents),
        "median_tokens": stats.median(a.get("tokens") for a in agents),
        "median_tokens_estimated": stats.median(a.get("tokens_estimated") for a in agents),
        "median_wall_s": _seconds(stats.median(a.get("wall_ms") for a in agents)),
    }


def _seconds(ms: float | None) -> float | None:
    return None if ms is None else round(ms / 1000, 1)


def _outcomes(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.get("outcome", "unknown")] = counts.get(record.get("outcome", "unknown"), 0) + 1
    return dict(sorted(counts.items()))


def _stop_reasons(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        reason = (record.get("agent") or {}).get("stop_reason")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _forge_block(records: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    forge = [r["forge"] for r in records if r.get("outcome") in ("success", "failure") and r.get("forge")]
    if not forge:
        return None
    rejected: dict[str, int] = {}
    created = [f.get("versions_created") for f in forge]
    for f in forge:
        for gate, count in (f.get("rejected_by_gate") or {}).items():
            rejected[gate] = rejected.get(gate, 0) + count
    reused = sum(1 for f in forge if f.get("reused"))
    return {
        "runs": len(forge),
        "reuse_rate": stats.rate(reused, len(forge)),
        "reuse_wilson_95": list(i) if (i := stats.wilson(reused, len(forge))) else None,
        "builds_attempted": sum(f.get("build_calls", 0) for f in forge),
        "median_builds": stats.median(f.get("build_calls") for f in forge),
        "median_server_tokens": stats.median(f.get("server_tokens") for f in forge),
        "median_build_rounds": stats.median(f.get("build_rounds") for f in forge),
        "versions_created": stats.total(created),
        "rejected_by_gate": dict(sorted(rejected.items())) if any(c is not None for c in created) else None,
    }


def _safety_block(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    counted = [r for r in records if r.get("outcome") in ("success", "failure")]
    safety = [r.get("safety") or {} for r in counted]
    return {
        "policy_violations": stats.total(s.get("policy_violations") for s in safety),
        "decoy_events": stats.total(s.get("decoy_events") for s in safety),
        "quarantines": stats.total(s.get("quarantines") for s in safety),
        "leaks": sum(len(r.get("leaks") or []) for r in counted) if counted else None,
        "runs_with_ledgers": sum(1 for s in safety if s.get("ledgers_read")),
    }


def block(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Every aggregate for one group of runs (a task, a tier, or everything) in one configuration."""
    out = stats.success_block(records)
    out.update(_medians(records))
    out["outcomes"] = _outcomes(records)
    out["stop_reasons"] = _stop_reasons(records)
    out["forge"] = _forge_block(records)
    out["safety"] = _safety_block(records)
    return out


def _single(values: Iterable[Any]) -> Any:
    found = sorted({v for v in values if v})
    if not found:
        return None
    return found[0] if len(found) == 1 else found


def comparisons(configs: dict[str, Any]) -> list[dict[str, Any]]:
    """Each pair of configurations, overall and per task: the two intervals and whether they leave room for no difference."""
    out = []
    for a, b in COMPARISONS:
        if a not in configs or b not in configs:
            continue
        scopes = [("overall", configs[a]["overall"], configs[b]["overall"])]
        scopes += [
            (task, configs[a]["tasks"][task], configs[b]["tasks"][task])
            for task in configs[a]["tasks"]
            if task in configs[b]["tasks"]
        ]
        for scope, left, right in scopes:
            overlap = stats.overlaps(left["wilson_95"], right["wilson_95"])
            out.append(
                {
                    "scope": scope,
                    "a": a,
                    "b": b,
                    "a_rate": left["success_rate"],
                    "b_rate": right["success_rate"],
                    "a_wilson_95": left["wilson_95"],
                    "b_wilson_95": right["wilson_95"],
                    "intervals_overlap": overlap,
                    "difference_supported": None if overlap is None else not overlap,
                }
            )
    return out


def attack_lab(document: dict[str, Any] | None) -> dict[str, Any]:
    """The Attack Lab line, from what `toolforge attacks run --json` wrote (P15). Every figure is null when no file was given."""
    empty = {
        "containment_rate": None,
        "wilson_95": None,
        "attacks": None,
        **{v: None for v in VERDICTS},
        "docker": None,
        "generated_at": None,
        "rule": ATTACK_LAB_RULE,
    }
    if not document:
        return {**empty, "note": ATTACK_LAB_MISSING}
    results = document.get("results")
    if not isinstance(results, list) or not all(
        isinstance(r, dict) and r.get("verdict") in VERDICTS for r in results
    ):
        raise ValueError(
            "not an Attack Lab result: expected `results`, each with a verdict, as `toolforge attacks run --json` writes"
        )
    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in VERDICTS}
    attempted = counts["contained"] + counts["breached"] + counts["inconclusive"]
    interval = stats.wilson(counts["contained"], attempted)
    return {
        **empty,
        "containment_rate": stats.rate(counts["contained"], attempted),
        "wilson_95": list(interval) if interval else None,
        "attacks": len(results),
        **counts,
        "docker": document.get("docker"),
        "generated_at": document.get("generated_at"),
        "note": "from the Attack Lab's own result file; it is a separate suite, not a benchmark configuration",
    }


def summarise(
    records: Sequence[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
    attacks: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """The `summary.json` document."""
    moment = now or dt.datetime.now(dt.UTC)
    configs: dict[str, Any] = {}
    order = [c for c in ("baseline", "forge_cold", "forge_warm") if any(r["config"] == c for r in records)]
    for config in order:
        mine = [r for r in records if r["config"] == config]
        task_ids = [t.id for t in task_set.TASKS if any(r["task"] == t.id for r in mine)]
        tiers = [t for t in task_set.TIERS if any(r.get("tier") == t for r in mine)]
        configs[config] = {
            "overall": block(mine),
            "tiers": {tier: block([r for r in mine if r.get("tier") == tier]) for tier in tiers},
            "tasks": {task: block([r for r in mine if r["task"] == task]) for task in task_ids},
        }
    return {
        "schema": SUMMARY_SCHEMA,
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": moment.strftime("%Y-%m-%d"),
        "model": _single(r.get("model") for r in records),
        "prompt_hash": _single(r.get("prompt_hash") for r in records) or prompt_hash(),
        "runs_total": len(records),
        "options": options,
        "operator": OPERATOR_NOTE,
        "configs": configs,
        "comparisons": comparisons(configs),
        "attack_lab": attack_lab(attacks),
        "context": context,  # None when `python -m bench context` was not run: never zeros
    }


# --- report.md -----------------------------------------------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "not measured" if value is None else f"{value * 100:.0f}%"


def _interval(value: Sequence[float] | None) -> str:
    return "n/a" if value is None else f"{value[0] * 100:.0f}% to {value[1] * 100:.0f}%"


def _num(value: Any) -> str:
    if value is None:
        return "not measured"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _row(name: str, b: dict[str, Any]) -> str:
    tokens = _num(b["median_tokens"])
    if b["median_tokens"] is None and b["median_tokens_estimated"] is not None:
        tokens = f"not reported (estimate {b['median_tokens_estimated']:g})"
    return (
        f"| {name} | {b['runs']} | {b['successes']} | {_pct(b['success_rate'])} | {_interval(b['wilson_95'])} | "
        f"{_num(b['median_tool_calls'])} | {tokens} | {_num(b['median_wall_s'])} | {b['not_measured']} |"
    )


TABLE_HEAD = (
    "| | Runs | Successes | Success rate | Wilson 95% | Median tool calls | Median tokens | Median time (s) | Not measured |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
)


def context_markdown(context: dict[str, Any] | None) -> list[str]:
    lines = ["## Context cost", ""]
    if not context:
        return lines + ["Not measured: `python -m bench context` was not run for this report.", ""]
    method = context["method"]
    label = "estimate" if method == "estimate" else "measured"
    lines += [
        f"Token method: **{label}** ({context['tokens_note']}). The registry is {context['registry']}; seed {context['seed']}. "
        "These numbers describe synthetic tools of the sizes below and say nothing about any other system.",
        "",
        "| Tools | meta: tools/list | meta: forge_find | meta: forge_describe | meta: total | direct: tools/list |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def cell(figure: dict[str, Any] | None) -> str:
        if not figure:
            return "not measured"
        tokens = "not measured" if figure["tokens"] is None else f"{figure['tokens']} tokens ({label})"
        return f"{figure['bytes']} B, {tokens}"

    for r in context["results"]:
        m, d = r["meta"], r["direct"]
        lines.append(
            f"| {r['size']} | {cell(m['tools_list'])} | {cell(m['find'])} | {cell(m['describe'])} | {cell(m['total'])} | "
            f"{cell(d['tools_list'])} |"
        )
    chains = [r for r in context["results"] if r.get("chain")]
    if chains:
        lines += ["", "| Tools | forge_chain (3 steps) | 3 x forge_run |", "| --- | --- | --- |"]
        for r in chains:
            lines.append(
                f"| {r['size']} | {cell(r['chain']['chain']['total'])} | {cell(r['chain']['separate_runs']['total'])} |"
            )
    elif not context.get("chain_measured"):
        lines += ["", "`forge_chain` (S02) is not built, so chains were not measured."]
    h = context.get("hypotheses") or {}
    lines += [
        "",
        "Hypotheses (stated in docs/BENCHMARK.md before measuring):",
        f"* meta `tools/list` is the same at every size: {_verdict(h.get('meta_tools_list_constant'))}",
        f"* one `forge_find` stays inside its budget: {_verdict(h.get('find_within_budget'))}",
        f"* direct `tools/list` grows with the number of tools: {_verdict(h.get('direct_grows_with_size'))} "
        f"(bytes added per tool at each size: {h.get('direct_bytes_per_tool')})",
        "",
    ]
    return lines


def attack_lab_markdown(lab: dict[str, Any]) -> list[str]:
    lines = ["## Attack Lab (separate line)", ""]
    if lab["containment_rate"] is None and lab["attacks"] is None:
        return lines + [f"Containment rate: not measured ({lab['note']}).", ""]
    rate = (
        "not measured"
        if lab["containment_rate"] is None
        else f"{_pct(lab['containment_rate'])} ({_interval(lab['wilson_95'])})"
    )
    return lines + [
        f"Containment rate: {rate}. Attacks: {lab['attacks']}; contained {lab['contained']}, breached {lab['breached']}, "
        f"inconclusive {lab['inconclusive']}, skipped {lab['skipped']}. Docker: {lab['docker'] or 'not recorded'}. "
        f"Result generated {lab['generated_at'] or 'at an unrecorded time'}.",
        "",
        f"Rule: {lab['rule']}. This is the hostile-author suite (docs/ATTACKS.md), not a benchmark configuration.",
        "",
    ]


def _verdict(value: bool | None) -> str:
    return "not measured" if value is None else ("held" if value else "did not hold")


def markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# ToolForge benchmark report",
        "",
        f"Generated {summary['generated_at']}. Model: `{summary['model'] or 'not recorded'}`. "
        f"Prompt hash: `{summary['prompt_hash']}`. Runs: {summary['runs_total']}.",
        "",
        f"{summary['operator'][0].upper()}{summary['operator'][1:]}. Method and hypotheses: docs/BENCHMARK.md.",
        "",
    ]
    configs = summary["configs"]
    if configs:
        lines += ["## Success by configuration", "", TABLE_HEAD]
        lines += [_row(f"`{c}`", configs[c]["overall"]) for c in configs]
        tiers = sorted({t for c in configs.values() for t in c["tiers"]}, key=task_set.TIERS.index)
        for tier in tiers:
            lines += ["", f"### Tier {tier}", "", TABLE_HEAD]
            lines += [
                _row(f"`{c}`", configs[c]["tiers"][tier]) for c in configs if tier in configs[c]["tiers"]
            ]
        lines += ["", "## Per task", ""]
        for config, data in configs.items():
            lines += [f"### `{config}`", "", TABLE_HEAD.replace("| |", "| Task |", 1)]
            lines += [_row(f"`{t}`", b) for t, b in data["tasks"].items()]
            lines.append("")
        forge = [(c, configs[c]["overall"]["forge"]) for c in configs if configs[c]["overall"]["forge"]]
        if forge:
            lines += [
                "## Building and reuse (forge configurations)",
                "",
                "| | Runs | Reuse rate | Wilson 95% | Builds attempted | Median builds | Median builder tokens | Versions created "
                "| Rejected, by first failed gate |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for config, f in forge:
                rejected = (
                    "not measured"
                    if f["rejected_by_gate"] is None
                    else (", ".join(f"{g}: {n}" for g, n in f["rejected_by_gate"].items()) or "none")
                )
                lines.append(
                    f"| `{config}` | {f['runs']} | {_pct(f['reuse_rate'])} | {_interval(f['reuse_wilson_95'])} | "
                    f"{f['builds_attempted']} | {_num(f['median_builds'])} | {_num(f['median_server_tokens'])} | "
                    f"{_num(f['versions_created'])} | {rejected} |"
                )
            lines.append("")
        lines += [
            "## Safety",
            "",
            "| | Policy violations | Decoy events | Quarantines | Leaks | Runs with ledgers |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for config, data in configs.items():
            s = data["overall"]["safety"]
            lines.append(
                f"| `{config}` | {_num(s['policy_violations'])} | {_num(s['decoy_events'])} | {_num(s['quarantines'])} | "
                f"{_num(s['leaks'])} | {s['runs_with_ledgers']} |"
            )
        lines += _comparison_markdown(summary["comparisons"])
    else:
        lines += ["No benchmark runs are in this report.", ""]
    lines += attack_lab_markdown(summary["attack_lab"])
    lines += context_markdown(summary.get("context"))
    lines += [
        "## What this shows and does not show",
        "",
        "* It shows how often one model, with one fixed prompt and budget, finished these tasks in each configuration, with "
        "intervals that are wide when the number of runs is small. Read the intervals, not the rates.",
        "* A difference is called supported above only when the two Wilson intervals do not overlap. Overlapping intervals "
        "mean the data cannot tell the configurations apart, not that they are equal.",
        "* Failed runs, harness errors and budget stops are all counted. Runs marked not measured could not be run here (no "
        "fixture site, or no sibling to warm with) and are counted apart, never as failures.",
        "* It does not show how other models, prompts, operators or tasks would do. The forge numbers depend on the scripted "
        "operator, which promotes without reading code.",
        "* Median tokens are what the provider reported for the agent's own requests. Tokens the server's builder model spends "
        "in forge_build and forge_repair are reported separately (median builder tokens): add both before comparing cost.",
        "* The context-cost figures are about synthetic registries and say nothing about any other system.",
        "",
    ]
    return "\n".join(lines)


def _comparison_markdown(rows: list[dict[str, Any]]) -> list[str]:
    overall = [r for r in rows if r["scope"] == "overall"]
    if not overall:
        return []
    lines = [
        "## Comparisons",
        "",
        "| | A | B | Intervals overlap | Difference supported |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in overall:
        lines.append(
            f"| `{r['a']}` vs `{r['b']}` | {_pct(r['a_rate'])} ({_interval(r['a_wilson_95'])}) | "
            f"{_pct(r['b_rate'])} ({_interval(r['b_wilson_95'])}) | {_num(r['intervals_overlap'])} | "
            f"{_num(r['difference_supported'])} |"
        )
    supported = [r for r in rows if r["difference_supported"]]
    lines += [
        "",
        f"Per-task comparisons are in summary.json; {len(supported)} of {len(rows)} comparisons have non-overlapping intervals.",
        "",
    ]
    return lines


def write(out: Path, summary: dict[str, Any]) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    summary_path, report_path = out / "summary.json", out / "report.md"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(markdown(summary), encoding="utf-8")
    return summary_path, report_path


def load_context(out: Path) -> dict[str, Any] | None:
    path = out / "context.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_attacks(out: Path) -> dict[str, Any] | None:
    """The Attack Lab result copied into the run directory as `attacks.json` (by `--attacks`), or None."""
    path = out / "attacks.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def store_attacks(out: Path, source: Path) -> Path:
    """Check that `source` is an Attack Lab result and copy it to `<out>/attacks.json`, so `report` can rebuild from the run alone."""
    document = json.loads(source.read_text(encoding="utf-8"))
    attack_lab(document)  # raises ValueError for anything else
    out.mkdir(parents=True, exist_ok=True)
    target = out / "attacks.json"
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


__all__ = [
    "attack_lab",
    "attack_lab_markdown",
    "block",
    "comparisons",
    "context_markdown",
    "load_attacks",
    "load_context",
    "markdown",
    "store_attacks",
    "summarise",
    "write",
]
