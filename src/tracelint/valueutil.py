"""Shared value normalization and scalar extraction (learning-doc 02 §2).

Provenance and dataflow checks must operate on **normalized values**, not raw string containment,
or they both over-trust and under-trust the trace (learning-doc 02 §2: a comma breaks a naive
match; a reused value for a different claim passes one). These helpers are the single normalizer
that R2 (dataflow reuse) and R3 (provenance derivability) share, so the two rules can never
disagree about whether two values are "the same."
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

_WS = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D")


def normalize(value: Any) -> str:
    """Case-fold, strip, and collapse whitespace — the canonical form for equality."""
    return _WS.sub(" ", str(value).strip().casefold())


def digits(value: Any) -> str:
    """The digit string of a value (so ``1,234.56`` and ``1234.56`` compare equal by digits)."""
    return _NON_DIGIT.sub("", str(value))


def iter_scalars(obj: Any) -> Iterator[Any]:
    """Yield every scalar (str / int / float, excluding bool) nested in ``obj``."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float, str)):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_scalars(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_scalars(v)


def significant_values(obj: Any) -> set[str]:
    """Normalized scalar values worth tracking across steps (ids, amounts, tokens).

    Trivial values (short strings, tiny numbers) are excluded so a coincidental match on ``"ok"``
    or ``0`` cannot ground a finding. Numbers and their string forms both normalize to ``str``.
    """
    out: set[str] = set()
    for s in iter_scalars(obj):
        if isinstance(s, str):
            t = s.strip()
            if len(t) >= 4:
                out.add(t)
        elif abs(s) >= 100:
            out.add(str(s))
    return out
