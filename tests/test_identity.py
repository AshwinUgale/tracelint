"""Stable finding fingerprints — the identity behind idempotent write-back and SARIF alerts."""

from __future__ import annotations

from tracelint.findings import ConfidenceTier, Finding
from tracelint.identity import finding_fingerprint


def _finding(rule="R2b", ftype="error_mishandled", tier=ConfidenceTier.HARD_DEFECT):
    return Finding(rule=rule, finding_type=ftype, tier=tier, summary="x")


def test_same_facts_same_id():
    f = _finding()
    a = finding_fingerprint(f, scope="t1", step_keys=["o3", "o5"])
    b = finding_fingerprint(f, scope="t1", step_keys=["o3", "o5"])
    assert a == b and len(a) == 16


def test_step_key_order_does_not_matter():
    f = _finding()
    assert finding_fingerprint(f, scope="t1", step_keys=["o3", "o5"]) == finding_fingerprint(
        f, scope="t1", step_keys=["o5", "o3"]
    )


def test_scope_changes_the_id():
    f = _finding()
    assert finding_fingerprint(f, scope="t1", step_keys=["o3"]) != finding_fingerprint(
        f, scope="t2", step_keys=["o3"]
    )


def test_rule_and_type_change_the_id():
    base = finding_fingerprint(_finding(), scope="t", step_keys=["o1"])
    assert base != finding_fingerprint(_finding(rule="R8"), scope="t", step_keys=["o1"])
    assert base != finding_fingerprint(
        _finding(ftype="duplicate_side_effect"), scope="t", step_keys=["o1"]
    )


def test_tier_and_summary_do_not_change_the_id():
    """Identity is which finding it is, not how we describe it."""
    hard = finding_fingerprint(
        _finding(tier=ConfidenceTier.HARD_DEFECT), scope="t", step_keys=["o1"]
    )
    cand = finding_fingerprint(_finding(tier=ConfidenceTier.CANDIDATE), scope="t", step_keys=["o1"])
    assert hard == cand
