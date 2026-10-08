"""The SWE-bench experiment's R2a audit summary (experiments/swebench/audit_r2a.py)."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from swebench.audit_r2a import summarize  # noqa: E402

LABELS = Path(__file__).resolve().parents[1] / "experiments" / "swebench" / "r2a_audit_labels.csv"


def _row(tier, label, sub="s1"):
    return {"submission": sub, "tier": tier, "label": label}


def test_strict_and_broad_precision_exclude_unclear():
    rows = [
        _row("hard_event", "error"),
        _row("hard_event", "expected_failure"),
        _row("hard_event", "informational"),
        _row("hard_event", "unclear"),
        _row("candidate", "informational"),
    ]
    summary = summarize(rows)
    hard = summary["hard_event/all"]
    assert hard["n"] == 3 and hard["unclear"] == 1
    assert hard["strict"]["p"] == round(1 / 3, 3) and hard["broad"]["p"] == round(2 / 3, 3)
    assert summary["candidate/all"]["strict"]["p"] == 0.0


def test_unlabeled_rows_are_rejected():
    with pytest.raises(ValueError, match="unlabeled"):
        summarize([_row("hard_event", "")])


def test_committed_labels_reproduce_the_published_numbers():
    with LABELS.open(encoding="utf-8") as fh:
        summary = summarize(list(csv.DictReader(fh)))
    assert summary["hard_event/all"]["strict"]["p"] == 0.596
    assert summary["hard_event/all"]["broad"]["p"] == 0.764
    assert summary["candidate/all"]["strict"]["p"] == 0.111


def test_held_out_labels_reproduce_the_published_numbers():
    with LABELS.with_name("r2a_heldout_labels.csv").open(encoding="utf-8") as fh:
        summary = summarize(list(csv.DictReader(fh)))
    assert summary["hard_event/all"]["broad"]["p"] == 0.933
    assert summary["hard_event/all"]["strict"]["p"] == 0.6
    assert summary["candidate/all"]["strict"]["p"] == 0.933
