"""Confidence intervals for rates (learning-doc 03 §5).

A rate computed over few runs (a finding's reproduction rate across ``k`` runs, a recovery rate
per fault type) must be reported with an interval, or it overstates certainty. For a **proportion**
the Wilson score interval is used — it stays inside ``[0, 1]`` and stays non-degenerate at the
extremes (``0/k``, ``k/k``) where the textbook Wald interval fails. For a **mean** (latency,
steps-to-recovery) the percentile bootstrap is used. Both are seeded / closed-form so results are
reproducible.
"""

from __future__ import annotations

import math
import random

Z_95 = 1.959963984540054  # standard normal quantile for a 95% interval


def wilson_interval(successes: int, n: int, *, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for ``successes`` out of ``n`` (learning-doc 03 §5).

    Worked check: ``wilson_interval(8, 10)`` ≈ ``(0.490, 0.943)`` — an honest "49%–94%" for an
    80% rate at n=10, where Wald would give an impossible upper bound above 1.
    """
    if n <= 0:
        return (0.0, 1.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def bootstrap_mean_ci(
    values: list[float],
    *,
    seed: int = 0,
    resamples: int = 2000,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean of ``values`` (seeded → reproducible)."""
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (float(values[0]), float(values[0]))
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo = means[int((alpha / 2) * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (lo, hi)
