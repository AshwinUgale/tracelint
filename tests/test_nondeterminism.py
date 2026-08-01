"""Phase 5 — nondeterminism: k runs, reproduction rates, flaky flags (spec §II.8)."""

from __future__ import annotations

from tracelint import ConfidenceTier, Finding, LintReport, aggregate_runs
from tracelint.nondeterminism import finding_key, lint_runs


def _f(rule, ft, tier, tool, step):
    return Finding(
        rule, ft, tier, f"{rule} on {tool}", evidence={"step_indices": [step], "tool": tool}
    )


def test_finding_key_ignores_step_position():
    a = _f("R4", "loop", ConfidenceTier.CANDIDATE, "search", 3)
    b = _f("R4", "loop", ConfidenceTier.CANDIDATE, "search", 11)
    assert finding_key(a) == finding_key(b)  # same defect, different position


def test_finding_key_distinguishes_tools():
    a = _f("R4", "loop", ConfidenceTier.CANDIDATE, "search", 3)
    b = _f("R4", "loop", ConfidenceTier.CANDIDATE, "lookup", 3)
    assert finding_key(a) != finding_key(b)


def test_consistent_finding_across_all_runs():
    reports = [
        LintReport("r1", [_f("R2a", "tool_error_event", ConfidenceTier.HARD_EVENT, "reserve", 2)]),
        LintReport("r2", [_f("R2a", "tool_error_event", ConfidenceTier.HARD_EVENT, "reserve", 4)]),
        LintReport("r3", [_f("R2a", "tool_error_event", ConfidenceTier.HARD_EVENT, "reserve", 2)]),
    ]
    agg = aggregate_runs(reports)
    assert agg.runs == 3 and agg.stable is True
    f = agg.findings[0]
    assert f.count == 3 and f.rate == 1.0 and f.flaky is False
    assert agg.consistent and not agg.flaky


def test_flaky_finding_in_a_subset_of_runs():
    reports = [
        LintReport("r1", [_f("R4", "loop", ConfidenceTier.CANDIDATE, "search", 3)]),
        LintReport("r2", []),  # did not reproduce
        LintReport("r3", [_f("R4", "loop", ConfidenceTier.CANDIDATE, "search", 5)]),
    ]
    agg = aggregate_runs(reports)
    f = agg.findings[0]
    assert f.count == 2 and f.runs == 3
    assert abs(f.rate - 2 / 3) < 1e-9
    assert f.flaky is True
    assert f.ci[0] < f.rate < f.ci[1] or f.ci[0] <= f.rate <= f.ci[1]  # CI brackets the rate
    assert agg.flaky and f in agg.flaky


def test_duplicate_finding_counted_once_per_run():
    # Two loop findings in ONE run should count as present-in-1-run, not 2.
    reports = [
        LintReport(
            "r1",
            [
                _f("R4", "loop", ConfidenceTier.CANDIDATE, "search", 3),
                _f("R4", "loop", ConfidenceTier.CANDIDATE, "search", 9),
            ],
        )
    ]
    agg = aggregate_runs(reports)
    assert agg.findings[0].count == 1


def test_single_run_is_unstable():
    agg = aggregate_runs([LintReport("r1", [])])
    assert agg.stable is False


def test_lint_runs_and_aggregate_end_to_end():
    from tracelint.agent import run_loop_demo
    from tracelint.rules import default_rules

    # Deterministic scripted agent → identical traces → the loop finding reproduces every run.
    traces_and_toolsets = [run_loop_demo() for _ in range(4)]
    traces = [t for t, _ in traces_and_toolsets]
    registry = traces_and_toolsets[0][1].to_registry()
    reports = lint_runs(traces, default_rules(), registry)
    agg = aggregate_runs(reports)
    loops = [f for f in agg.findings if f.finding_type == "loop"]
    assert loops and loops[0].count == 4 and loops[0].flaky is False
