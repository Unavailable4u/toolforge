"""`python -m bench`: run the benchmark, measure context cost, rebuild a report, or list the tasks.

    python -m bench --config all --runs 5 --out runs/bench/<timestamp>/
    python -m bench --config baseline --tasks log_stats --runs 2
    python -m bench context --sizes 10,100,1000 --seed 1 [--endpoint URL --model ID] [--out DIR]
    python -m bench report --out DIR [--attacks runs/attacks/attacks-<time>.json]
    python -m bench tasks

The agent's model is the builder model from the usual settings (TOOLFORGE_LLM_BASE_URL, TOOLFORGE_LLM_MODEL, TOOLFORGE_LLM_API_KEY);
`toolforge.config` is what reads them. Runs need the sandbox (Docker and the runtime image); `context`, `report` and `tasks` need
neither Docker nor a model.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from bench import tasks as task_set
from bench.agent import DEFAULT_MAX_SECONDS, DEFAULT_MAX_STEPS, DEFAULT_MAX_TOKENS, Budget

SUBCOMMANDS = ("context", "report", "tasks")
ATTACKS_HELP = "an Attack Lab result (`toolforge attacks run --json` writes runs/attacks/attacks-<time>.json) for its separate line"


def _default_out() -> Path:
    return Path("runs") / "bench" / dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench", description="Run the ToolForge benchmark (docs/BENCHMARK.md)."
    )
    parser.add_argument(
        "--config", default="all", help="all, or a comma list of: baseline, forge_cold, forge_warm"
    )
    parser.add_argument(
        "--tasks",
        default="",
        help="a comma list of task ids (default: every task; see `python -m bench tasks`)",
    )
    parser.add_argument(
        "--runs", type=int, default=5, help="runs per configuration and task; run n uses seed + n (default 5)"
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--out", type=Path, default=None, help="output directory (default runs/bench/<timestamp>/)"
    )
    parser.add_argument(
        "--steps", type=int, default=DEFAULT_MAX_STEPS, help="tool calls per run (default 20)"
    )
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="agent tokens per run")
    parser.add_argument(
        "--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, help="wall-clock seconds per run"
    )
    parser.add_argument(
        "--fixture-host", default=None, help="the host name that reaches the fixture server from the sandbox"
    )
    parser.add_argument("--fixture-bind", default="127.0.0.1", help="address the fixture server listens on")
    parser.add_argument(
        "--fixture-port",
        type=int,
        default=0,
        help="port the fixture server listens on (default: any free one)",
    )
    parser.add_argument(
        "--keep-homes", action="store_true", help="keep each run's TOOLFORGE_HOME (for debugging)"
    )
    parser.add_argument(
        "--with-context", action="store_true", help="also run the context measurement into the same report"
    )
    parser.add_argument("--attacks", type=Path, default=None, help=ATTACKS_HELP)
    return parser


def context_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench context", description="Measure what the server's answers cost."
    )
    parser.add_argument("--sizes", default="10,100,1000", help="registry sizes, comma separated")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--endpoint", default=None, help="an OpenAI-compatible base URL to count tokens with (else: estimate)"
    )
    parser.add_argument("--model", default=None, help="the model id for --endpoint")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write context.json and rebuild summary.json and report.md here",
    )
    return parser


def _counter(args: argparse.Namespace):
    from bench import context

    if not args.endpoint:
        return context.Counter()
    if not args.model:
        raise SystemExit("--endpoint needs --model")
    from toolforge.config import load_settings

    settings = load_settings()
    key = None
    if settings.llm_api_key is not None and (settings.llm_base_url or "").rstrip("/") == args.endpoint.rstrip(
        "/"
    ):
        key = (
            settings.llm_api_key.get_secret_value()
        )  # the configured key goes only to the configured endpoint
    return context.EndpointCounter(args.endpoint, args.model, key)


def _store_attacks(out: Path, source: Path | None) -> None:
    from bench import report

    if source is None:
        return
    try:
        report.store_attacks(out, source)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--attacks {source}: {exc}") from None


def _rebuild(out: Path, options: dict | None = None) -> None:
    from bench import harness, report

    summary = report.summarise(
        harness.load_runs(out) if (out / "runs").is_dir() else [],
        context=report.load_context(out),
        attacks=report.load_attacks(out),
        options=options,
    )
    summary_path, report_path = report.write(out, summary)
    print(f"wrote {summary_path} and {report_path}")


def cmd_context(argv: Sequence[str]) -> int:
    from bench import context

    args = context_parser().parse_args(argv)
    try:
        sizes = [int(s) for s in _csv(args.sizes)]
    except ValueError:
        raise SystemExit("--sizes must be whole numbers separated by commas") from None
    block = context.measure(sizes, args.seed, _counter(args))
    if args.out is None:
        print(json.dumps(block, indent=2, sort_keys=True))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "context.json").write_text(
        json.dumps(block, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _rebuild(args.out)
    return 0


def cmd_report(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench report", description="Rebuild summary.json and report.md."
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--attacks", type=Path, default=None, help=ATTACKS_HELP)
    args = parser.parse_args(argv)
    _store_attacks(args.out, args.attacks)
    previous = args.out / "summary.json"
    options = json.loads(previous.read_text(encoding="utf-8")).get("options") if previous.is_file() else None
    _rebuild(args.out, options)
    return 0


def cmd_tasks(argv: Sequence[str]) -> int:
    print(task_set.tasks_markdown())
    return 0


def cmd_run(argv: Sequence[str]) -> int:
    from bench import harness

    args = run_parser().parse_args(argv)
    configs = harness.CONFIGS if args.config == "all" else tuple(_csv(args.config))
    try:
        options = harness.Options(
            configs=configs,
            task_ids=tuple(_csv(args.tasks)),
            runs=args.runs,
            seed=args.seed,
            budget=Budget(max_steps=args.steps, max_tokens=args.max_tokens, max_seconds=args.max_seconds),
            fixture_host=args.fixture_host,
            fixture_bind=args.fixture_bind,
            fixture_port=args.fixture_port,
            keep_homes=args.keep_homes,
        )
    except (ValueError, KeyError) as exc:
        raise SystemExit(str(exc).strip("'\"")) from None
    from toolforge.config import load_settings

    try:
        lab = harness.RealLab(load_settings())
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    out = args.out or _default_out()
    _store_attacks(out, args.attacks)  # checked before any run starts
    harness.run(lab, options, out, progress=print)
    if args.with_context:
        from bench import context

        (out / "context.json").write_text(
            json.dumps(context.measure(list(context.DEFAULT_SIZES), args.seed), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    _rebuild(
        out,
        {
            "configs": list(options.configs),
            "tasks": [t.id for t in options.tasks()],
            "runs": options.runs,
            "seed": options.seed,
            "max_steps": options.budget.max_steps,
            "max_tokens": options.budget.max_tokens,
            "max_seconds": options.budget.max_seconds,
            "fixture_host": options.fixture_host,
        },
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in SUBCOMMANDS:
        return {"context": cmd_context, "report": cmd_report, "tasks": cmd_tasks}[args[0]](args[1:])
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
