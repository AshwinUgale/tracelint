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
from decimal import Decimal
from typing import Any

_WS = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D")
_NON_ALNUM = re.compile(r"[\W_]+")


def http_status_code(value: Any) -> int | None:
    """An HTTP status as an int — ``404`` or ``"404"``; anything else (``True``, ``"n/a"``) is not
    a status."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def normalize(value: Any) -> str:
    """Case-fold, strip, and collapse whitespace — the canonical form for equality."""
    return _WS.sub(" ", str(value).strip().casefold())


def digits(value: Any) -> str:
    """The digit string of a value (so ``1,234.56`` and ``1234.56`` compare equal by digits)."""
    return _NON_DIGIT.sub("", str(value))


def compact(value: Any) -> str:
    """Letters and digits only, case-folded (so ``A-100``, ``a100`` and ``A 100`` compare equal)."""
    return _NON_ALNUM.sub("", str(value).casefold())


_DECIMAL = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"  # 1200, 1,200, -3.5
_NUMBER_VALUE = re.compile(rf"[$€£¥]?\s*({_DECIMAL})")
# A number written in text: not part of a word or a dotted version (v1.2.3).
_NUMBER_IN_TEXT = re.compile(rf"(?<![\w.]){_DECIMAL}(?!\w|\.\d)")


def number(value: Any) -> Decimal | None:
    """``value`` as an exact number if it is one (``1200``, ``1200.0``, ``"1,200"``,
    ``"$1,200.00"`` are all 1200), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        found = Decimal(value)
    elif isinstance(value, float):
        found = Decimal(repr(value))  # the shortest decimal that round-trips, not binary noise
    elif isinstance(value, str):
        match = _NUMBER_VALUE.fullmatch(value.strip())
        if match is None:
            return None
        found = Decimal(match.group(1).replace(",", ""))
    else:
        return None
    return found if found.is_finite() else None


def numbers_in(text: str) -> Iterator[Decimal]:
    """The numbers written in ``text``."""
    for match in _NUMBER_IN_TEXT.finditer(text):
        yield Decimal(match.group().replace(",", ""))


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
