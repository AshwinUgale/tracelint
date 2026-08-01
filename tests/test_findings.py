"""Phase 0 — the uniform finding shape and the CI exit-code contract (spec §II.4, §II.10)."""

from __future__ import annotations

from tracelint import ConfidenceTier, Finding, LintReport
from tracelint.findings import EXIT_HARD_DEFECT, EXIT_OK


def _finding(tier: ConfidenceTier, rule: str = "R1", ft: str = "schema_violation") -> Finding:
    return Finding(
        rule=rule, finding_type=ft, tier=tier, summary="x", evidence={"step_indices": [3]}
    )


def test_tier_and_type_are_orthogonal():
    # Same finding_type can appear at different tiers (structured vs heuristic error signal).
    hard = Finding("R2a", "tool_error_event", ConfidenceTier.HARD_EVENT, "http 500")
    cand = Finding("R2a", "tool_error_event", ConfidenceTier.CANDIDATE, "exception-like text")
    assert hard.finding_type == cand.finding_type
    assert hard.tier != cand.tier


def test_step_indices_helper():
    f = _finding(ConfidenceTier.HARD_DEFECT)
    assert f.step_indices == [3]
    assert Finding("R", "t", ConfidenceTier.CANDIDATE, "s").step_indices == []


def test_hard_defect_drives_nonzero_exit():
    report = LintReport("run", [_finding(ConfidenceTier.HARD_DEFECT)])
    assert report.has_hard_defect is True
    assert report.exit_code == EXIT_HARD_DEFECT


def test_events_and_candidates_do_not_fail_ci():
    report = LintReport(
        "run",
        [_finding(ConfidenceTier.HARD_EVENT), _finding(ConfidenceTier.CANDIDATE)],
    )
    assert report.has_hard_defect is False
    assert report.exit_code == EXIT_OK
    assert len(report.by_tier(ConfidenceTier.CANDIDATE)) == 1


def test_empty_report_is_clean():
    assert LintReport("run", []).exit_code == EXIT_OK


def test_suppression_does_not_count_as_a_defect():
    report = LintReport(
        "run",
        [
            Finding.suppressed("R3", "hallucinated_arg", "no provenance captured"),
            _finding(ConfidenceTier.HARD_DEFECT),
        ],
    )
    assert len(report.suppressions) == 1
    assert len(report.active_findings) == 1
    # A hard_defect still fails; a suppression alongside it never masks or inflates the exit.
    assert report.exit_code == EXIT_HARD_DEFECT


def test_suppression_only_report_is_clean_but_disclosed():
    report = LintReport("run", [Finding.suppressed("R3", "hallucinated_arg", "missing schema")])
    assert report.exit_code == EXIT_OK  # cannot-check is not a defect...
    assert report.suppressions[0].is_suppression  # ...but it is disclosed, not hidden.


def test_finding_round_trip():
    f = _finding(ConfidenceTier.CANDIDATE)
    f.possible_false_positive = True
    back = Finding.from_dict(f.to_dict())
    assert back.tier is ConfidenceTier.CANDIDATE
    assert back.possible_false_positive is True
    assert back.step_indices == [3]
