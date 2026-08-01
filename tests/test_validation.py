"""Phase 7 — the constructed validation suite recovers every planted defect (spec §II.11)."""

from __future__ import annotations

import pytest

from tracelint.rules import default_rules, lint_trace
from tracelint.validation import validation_cases

CASES = validation_cases()


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_case_behaves_as_expected(case):
    report = lint_trace(case.trace, default_rules(), case.registry)
    assert case.check(report), (
        f"{case.name}: expected {case.expectation!r}; "
        f"got {[(f.rule, f.tier.value) for f in report.active_findings]}"
    )


def test_every_defect_type_is_represented():
    planted = {c.name for c in CASES if c.kind == "planted"}
    # One planted instance for each rule family (R3 twice: candidate + hard).
    for rule in ("r1", "r2a", "r2b", "r3", "r4", "r5"):
        assert any(name.startswith(rule) for name in planted), f"no planted case for {rule}"


def test_suite_has_controls_and_suspicious_cases():
    kinds = {c.kind for c in CASES}
    assert {"planted", "control", "suspicious"} <= kinds


def test_all_cases_pass_as_a_self_check():
    # The whole suite must be internally consistent (used by `tracelint demo` as a smoke test).
    for case in CASES:
        report = lint_trace(case.trace, default_rules(), case.registry)
        assert case.check(report), case.name
