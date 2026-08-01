"""Phase 5 — Wilson / bootstrap confidence intervals (learning-doc 03 §5)."""

from __future__ import annotations

from tracelint import bootstrap_mean_ci, wilson_interval


def test_wilson_worked_example_8_of_10():
    lo, hi = wilson_interval(8, 10)
    assert abs(lo - 0.490) < 0.005  # the learning-doc worked value
    assert abs(hi - 0.943) < 0.005


def test_wilson_all_successes_is_bounded_not_degenerate():
    lo, hi = wilson_interval(10, 10)
    assert 0.97 < hi <= 1.0  # near 1 but a real interval, not Wald's degenerate [1, 1]
    assert 0.6 < lo < 0.8  # lower bound well below 1 — honest uncertainty at n=10


def test_wilson_zero_successes():
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0.0 < hi < 0.4


def test_wilson_zero_n_is_maximally_uncertain():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_narrows_with_more_samples():
    _, hi_small = wilson_interval(4, 5)
    _, hi_large = wilson_interval(80, 100)
    width_small = hi_small - wilson_interval(4, 5)[0]
    width_large = hi_large - wilson_interval(80, 100)[0]
    assert width_large < width_small  # same rate, more data → tighter interval


def test_bootstrap_is_seeded_and_brackets_the_mean():
    values = [10.0, 12.0, 9.0, 11.0, 30.0]
    lo, hi = bootstrap_mean_ci(values, seed=1)
    assert lo <= sum(values) / len(values) <= hi
    # Reproducible with the same seed.
    assert bootstrap_mean_ci(values, seed=1) == (lo, hi)


def test_bootstrap_single_value():
    assert bootstrap_mean_ci([7.0]) == (7.0, 7.0)


def test_bootstrap_empty():
    assert bootstrap_mean_ci([]) == (0.0, 0.0)
