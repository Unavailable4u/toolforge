"""Benchmark statistics (P16): the Wilson interval against known values, medians that keep "not measured" apart from zero."""

from __future__ import annotations

import pytest

from bench import stats


@pytest.mark.parametrize(
    ("successes", "trials", "expected"),
    [
        # Reference values: Wilson score interval, z = 1.96 (Newcombe 1998, Table I; and any statistics package).
        (8, 10, (0.4902, 0.9433)),
        (5, 10, (0.2366, 0.7634)),
        (0, 5, (0.0, 0.4345)),
        (5, 5, (0.5655, 1.0)),
        (0, 1, (0.0, 0.7935)),
        (81, 263, (0.2553, 0.3662)),
    ],
)
def test_wilson_matches_known_values(successes: int, trials: int, expected: tuple[float, float]) -> None:
    low, high = stats.wilson(successes, trials)
    assert low == pytest.approx(expected[0], abs=1e-4) and high == pytest.approx(expected[1], abs=1e-4)


def test_wilson_stays_inside_zero_and_one_and_narrows_with_more_runs() -> None:
    for n in (1, 2, 5, 20, 100):
        for k in range(n + 1):
            low, high = stats.wilson(k, n)
            assert 0.0 <= low <= k / n <= high <= 1.0
    assert (
        stats.wilson(50, 100)[1] - stats.wilson(50, 100)[0] < stats.wilson(5, 10)[1] - stats.wilson(5, 10)[0]
    )


def test_wilson_of_no_runs_is_none_and_bad_counts_raise() -> None:
    assert stats.wilson(0, 0) is None
    with pytest.raises(ValueError):
        stats.wilson(3, 2)
    with pytest.raises(ValueError):
        stats.wilson(-1, 2)


def test_median_ignores_unmeasured_values_and_is_none_for_nothing() -> None:
    assert stats.median([3, 1, 2]) == 2.0
    assert stats.median([4, 1, 3, 2]) == 2.5
    assert stats.median([None, 5, None]) == 5.0
    assert stats.median([]) is None and stats.median([None, None]) is None
    assert stats.median([0, 0]) == 0.0  # a measured zero is a zero


def test_total_and_rate_keep_none_apart_from_zero() -> None:
    assert stats.total([None, None]) is None
    assert stats.total([0, None]) == 0
    assert stats.total([1, 2, None]) == 3
    assert stats.rate(0, 0) is None and stats.rate(0, 4) == 0.0 and stats.rate(1, 3) == 0.3333


def test_overlap() -> None:
    assert stats.overlaps([0.1, 0.4], [0.3, 0.9]) is True
    assert stats.overlaps([0.1, 0.2], [0.3, 0.9]) is False
    assert stats.overlaps([0.1, 0.3], [0.3, 0.9]) is True  # touching counts as overlapping
    assert stats.overlaps(None, [0.1, 0.2]) is None


def test_success_block_counts_not_measured_runs_apart() -> None:
    records = [
        {"outcome": "success", "success": True},
        {"outcome": "failure", "success": False},
        {"outcome": "error", "success": False},
        {"outcome": "not_measured", "success": None},
    ]
    block = stats.success_block(records)
    assert block["runs"] == 3 and block["successes"] == 1 and block["not_measured"] == 1
    assert block["success_rate"] == 0.3333 and block["wilson_95"] == list(stats.wilson(1, 3))
    empty = stats.success_block([{"outcome": "not_measured", "success": None}])
    assert empty["runs"] == 0 and empty["success_rate"] is None and empty["wilson_95"] is None
