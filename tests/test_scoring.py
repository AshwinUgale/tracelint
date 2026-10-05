"""The shared scoring core (used by both the DeepEval and promptfoo wrappers)."""

from __future__ import annotations

import pytest

from tracelint.findings import ConfidenceTier
from tracelint.integrations import deepeval, promptfoo, scoring


def test_as_tier_accepts_name_or_tier():
    assert scoring.as_tier("hard_event") is ConfidenceTier.HARD_EVENT
    assert scoring.as_tier(ConfidenceTier.CANDIDATE) is ConfidenceTier.CANDIDATE
    with pytest.raises(ValueError):
        scoring.as_tier("not-a-tier")


def test_both_wrappers_share_one_core():
    # the refactor must not fork the logic: each wrapper reuses scoring's own objects.
    assert deepeval.score_trace is scoring.score_trace
    assert deepeval.resolve_trace is scoring.resolve_trace
    assert deepeval.resolve_registry is scoring.resolve_registry
    assert deepeval.TracelintScore is scoring.TracelintScore
    assert promptfoo.score_trace is scoring.score_trace
    assert promptfoo.resolve_trace is scoring.resolve_trace


def test_resolve_trace_rejects_a_bad_type():
    with pytest.raises(ValueError, match="Trace or a path"):
        scoring.resolve_trace(123)  # type: ignore[arg-type]
