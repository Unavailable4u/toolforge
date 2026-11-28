"""Run the ToolForge demo (docs/DEMO_SCRIPT.md).

    python demo/run_demo.py --replay demo/transcripts/                      # scripted agent, real everything else
    python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/main/
    python demo/run_demo.py --live                                          # a real model (TOOLFORGE_LLM_*)
    python demo/run_demo.py --replay demo/transcripts/ --acts 1,2           # some acts (act 2 needs act 1)
    python demo/run_demo.py --replay demo/transcripts/ --sandbox stand-in   # no Docker, NO isolation: development only

Needs a checkout (it imports `bench/`), Docker with the runtime image built (`toolforge images build`) unless `--sandbox stand-in`, and
no language model in replay mode. Exit code 0 when every act's answer was accepted, 1 when one was not, 2 when it could not start.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import acts, runner  # noqa: E402
from demo.replay import ReplayError  # noqa: E402
from demo.standin import StandInUnavailable  # noqa: E402


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the ToolForge demo.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--replay", metavar="DIR", help="play the scripted agent turns in DIR (no language model)"
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="use the configured model (TOOLFORGE_LLM_BASE_URL, TOOLFORGE_LLM_MODEL)",
    )
    parser.add_argument("--acts", default="all", help="all, or a list such as 1,2 or forge,attack")
    parser.add_argument("--sandbox", choices=runner.SANDBOXES, default="docker")
    parser.add_argument("--record", metavar="DIR", help="also write a sanitised recording (3 files) to DIR")
    parser.add_argument("--out", default="runs/demo", help="where the run folder goes (default runs/demo)")
    parser.add_argument("--seed", type=int, default=acts.SEED)
    parser.add_argument(
        "--pace", type=float, default=0.0, metavar="SECONDS", help="pause between steps, for screen recording"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    try:
        options = runner.Options(
            mode="live" if args.live else "replay",
            transcripts=Path(args.replay) if args.replay else None,
            sandbox=args.sandbox,
            acts=acts.select(args.acts),
            out=Path(args.out),
            record=Path(args.record) if args.record else None,
            seed=args.seed,
            pace_s=args.pace,
        )
        code, _ = runner.run(options)
        return code
    except (runner.DemoError, ReplayError, StandInUnavailable, ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
