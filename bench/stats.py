"""Small, exact statistics for the benchmark: the Wilson score interval, medians, and how runs are grouped.

Everything here is pure arithmetic over run records, so it is tested against known values (`tests/test_bench_stats.py`). A value that was
not measured is `None` and stays `None`: it is never counted as zero, never averaged in, and a median of nothing is `None`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

Z_95 = 1.959963984540054  # the two-sided 95 percent quantile of the standard normal distribution


def wilson(successes: int, trials: int, z: float = Z_95) -> tuple[float, float] | None:
    """The Wilson score interval for a binomial proportion, as `(low, high)` rounded to four places; `None` for zero trials.

    Wilson rather than the normal approximation: it stays inside [0, 1] and is sensible for small samples and for 0 or n successes, which
    is exactly where a benchmark with five runs lives.
    """
    if trials <= 0:
        return None
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and the number of trials")
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def median(values: Iterable[float | int | None]) -> float | None:
    """The median of the measured values (the mean of the two middle ones for an even count); `None` when nothing was measured."""
    known = sorted(v for v in values if v is not None)
    if not known:
        return None
    middle = len(known) // 2
    if len(known) % 2:
        return float(known[middle])
    return (known[middle - 1] + known[middle]) / 2


def total(values: Iterable[int | None]) -> int | None:
    """The sum of the measured values, `None` when none was measured."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def rate(successes: int, trials: int) -> float | None:
    return round(successes / trials, 4) if trials else None


def overlaps(a: Sequence[float] | None, b: Sequence[float] | None) -> bool | None:
    """Whether two intervals overlap; `None` when either is missing. The report calls a difference supported only when they do not."""
    if a is None or b is None:
        return None
    return not (a[1] < b[0] or b[1] < a[0])


def success_block(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Success over the measured runs among `records`. Runs that could not be measured are counted apart and never as failures."""
    measured = [r for r in records if r.get("outcome") != "not_measured"]
    wins = sum(1 for r in measured if r.get("success") is True)
    return {
        "runs": len(measured),
        "successes": wins,
        "success_rate": rate(wins, len(measured)),
        "wilson_95": list(interval) if (interval := wilson(wins, len(measured))) else None,
        "not_measured": len(records) - len(measured),
    }


__all__ = ["Z_95", "median", "overlaps", "rate", "success_block", "total", "wilson"]
