"""Write `site/data/results.json`: the only numbers the website may show (P19).

    python scripts/build_site_data.py                      # reads runs/ and demo/recordings/main/, writes site/data/results.json
    python scripts/build_site_data.py --runs DIR --out FILE --generated-at 2026-11-28

It reads, newest first and never anything else:

* `runs/attacks/attacks-*.json`          the Attack Lab (P15, `toolforge attacks run --json`)
* `runs/bench/*/summary.json`            the benchmark (P16, `python -m bench`) and its context figures
* `demo/recordings/main/recording.json`  metadata of the recorded demo (P18)

A source that is missing, unreadable or not in the shape this script knows becomes `null` and the page draws its hatched "Not measured
yet" state. A number is never replaced by zero. Only the standard library is needed to run it; when the repository's own `demo.recording`
can be imported, the recording is validated with it before its metadata is used.

Proven by `tests/test_site_data.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS = ROOT / "runs"
DEFAULT_OUT = ROOT / "site" / "data" / "results.json"
DEMO_DIR = ROOT / "demo" / "recordings" / "main"
SCHEMA = 1
CONFIG_ORDER = ("baseline", "forge_cold", "forge_warm")
RATE_KEYS = ("runs", "successes", "success_rate", "wilson_95", "not_measured")


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _newest(paths: list[Path]) -> list[Path]:
    """Newest first. The names carry a UTC timestamp, so a name sort is a time sort and needs no clock."""
    return sorted(paths, key=lambda p: p.as_posix(), reverse=True)


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rates(block: Any) -> dict[str, Any] | None:
    """The figures of one success block, kept as the run wrote them (`None` stays `None`)."""
    if not isinstance(block, dict) or _number(block.get("runs")) is None:
        return None
    return {key: block.get(key) for key in RATE_KEYS}


# --- Attack Lab ---------------------------------------------------------------------------------------------------------------


def load_attacks(runs: Path) -> dict[str, Any] | None:
    for path in _newest(list((runs / "attacks").glob("attacks-*.json"))):
        doc = _read_json(path)
        results = doc.get("results") if isinstance(doc, dict) else None
        if not isinstance(results, list) or not results:
            continue
        per_attack: dict[str, str] = {}
        for row in results:
            if (
                isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and isinstance(row.get("verdict"), str)
            ):
                per_attack[row["id"]] = row["verdict"]
        if len(per_attack) != len(results):
            continue
        counts = {v: sum(1 for x in per_attack.values() if x == v) for v in set(per_attack.values())}
        return {
            "run_id": path.stem,
            "generated_at": doc.get("generated_at"),
            "toolforge": doc.get("toolforge"),
            "docker": doc.get("docker"),
            "contained": counts.get("contained", 0),
            "breached": counts.get("breached", 0),
            "inconclusive": counts.get("inconclusive", 0),
            "skipped": counts.get("skipped", 0),
            "total": len(per_attack),
            "per_attack": dict(sorted(per_attack.items())),
            "footer": doc.get("footer"),
        }
    return None


# --- Benchmark and context ----------------------------------------------------------------------------------------------------


def _bench_block(run_dir: Path, summary: dict[str, Any]) -> dict[str, Any] | None:
    configs_in = summary.get("configs")
    if not isinstance(configs_in, dict) or not configs_in:
        return None
    configs: dict[str, Any] = {}
    for name in sorted(configs_in, key=lambda n: (CONFIG_ORDER.index(n) if n in CONFIG_ORDER else 99, n)):
        config = configs_in[name]
        overall = _rates(config.get("overall")) if isinstance(config, dict) else None
        if overall is None:
            continue
        tiers = {}
        for tier, block in sorted((config.get("tiers") or {}).items()):
            if (rates := _rates(block)) is not None:
                tiers[str(tier)] = rates
        configs[name] = {"overall": overall, "tiers": tiers}
    if not configs:
        return None
    comparisons = []
    for row in summary.get("comparisons") or []:
        if isinstance(row, dict) and row.get("scope") == "overall":
            comparisons.append(
                {
                    key: row.get(key)
                    for key in ("a", "b", "scope", "intervals_overlap", "difference_supported")
                    if key in row
                }
            )
    return {
        "run_id": run_dir.name,
        "date": summary.get("date"),
        "model": summary.get("model"),
        "runs": max(c["overall"]["runs"] for c in configs.values()),
        "runs_total": summary.get("runs_total"),
        "configs": configs,
        "comparisons": comparisons,
    }


def _figure(block: Any) -> dict[str, Any] | None:
    if not isinstance(block, dict) or _number(block.get("bytes")) is None:
        return None
    return {"bytes": block["bytes"], "tokens": block.get("tokens")}


def _context_block(summary: dict[str, Any], date: Any) -> dict[str, Any] | None:
    context = summary.get("context")
    if not isinstance(context, dict) or context.get("method") not in ("estimate", "measured"):
        return None
    sizes: dict[str, Any] = {}
    for row in context.get("results") or []:
        if not isinstance(row, dict) or not isinstance(row.get("size"), int):
            continue
        meta, direct = row.get("meta") or {}, row.get("direct") or {}
        total, listing = _figure(meta.get("total")), _figure(direct.get("tools_list"))
        if total is None or listing is None:
            continue
        sizes[str(row["size"])] = {"meta": total, "direct": listing}
    if not sizes:
        return None
    return {"method": context["method"], "model": context.get("model"), "date": date, "sizes": sizes}


def load_bench_and_context(runs: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The newest run that has a benchmark, and the newest context figures from any run (they are made by a separate command)."""
    bench: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    for path in _newest(list((runs / "bench").glob("*/summary.json"))):
        summary = _read_json(path)
        if not isinstance(summary, dict) or summary.get("schema") != 1:
            continue
        if bench is None:
            bench = _bench_block(path.parent, summary)
        if context is None:
            context = _context_block(summary, summary.get("date"))
        if bench is not None and context is not None:
            break
    return bench, context


# --- Demo ---------------------------------------------------------------------------------------------------------------------


def _validate_recording(directory: Path) -> list[str]:
    """Problems found by the repository's own check, or none when it cannot be imported (this script has no dependency of its own)."""
    sys.path.insert(0, str(ROOT))
    try:
        from demo import recording  # noqa: PLC0415

        return list(recording.validate(directory))
    except ImportError:
        return []
    finally:
        sys.path.remove(str(ROOT))


def load_demo(directory: Path = DEMO_DIR) -> dict[str, Any] | None:
    meta = _read_json(directory / "recording.json")
    if not isinstance(meta, dict) or not meta.get("recorded_at") or not meta.get("toolforge_version"):
        return None
    problems = _validate_recording(directory)
    if problems:
        print(f"build_site_data: the demo recording is not used: {problems[0]}", file=sys.stderr)
        return None
    return {
        "recorded_at": meta["recorded_at"],
        "toolforge_version": meta["toolforge_version"],
        "mode": meta.get("mode"),
        "agent": meta.get("agent"),
        "sandbox": meta.get("sandbox"),
        "isolated": meta.get("isolated"),
    }


# --- The file -----------------------------------------------------------------------------------------------------------------


def build(runs: Path, generated_at: str, demo_dir: Path = DEMO_DIR) -> dict[str, Any]:
    bench, context = load_bench_and_context(runs)
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "attacks": load_attacks(runs),
        "bench": bench,
        "context": context,
        "demo": load_demo(demo_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write site/data/results.json from runs/ and the demo recording."
    )
    parser.add_argument(
        "--runs",
        type=Path,
        default=DEFAULT_RUNS,
        help="directory holding attacks/ and bench/ (default: runs/)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--demo", type=Path, default=DEMO_DIR, help="the recording directory")
    parser.add_argument("--generated-at", default=None, help="a date, YYYY-MM-DD (default: today, UTC)")
    args = parser.parse_args(argv)
    generated_at = args.generated_at or datetime.now(UTC).strftime("%Y-%m-%d")
    document = build(args.runs, generated_at, args.demo)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    present = [key for key in ("attacks", "bench", "context", "demo") if document[key] is not None]
    missing = [key for key in ("attacks", "bench", "context", "demo") if document[key] is None]
    print(
        f"wrote {args.out}: found {', '.join(present) or 'nothing'}; not measured yet: {', '.join(missing) or 'nothing'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
