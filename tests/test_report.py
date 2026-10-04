"""Phase 1c — text report rendering (spec §II.10)."""

from __future__ import annotations

from tracelint import ConfidenceTier, Finding, LintReport, render_report, render_reports


def _defect() -> Finding:
    return Finding(
        "R1",
        "schema_violation",
        ConfidenceTier.HARD_DEFECT,
        "'cancel_order' call violates its schema (1 error: type)",
        evidence={
            "step_indices": [1],
            "tool": "cancel_order",
            "errors": [{"path": "/order_id", "keyword": "type", "message": "4521 is not a string"}],
        },
    )


def test_report_shows_defect_with_location_and_errors():
    text = render_report(LintReport("planted", [_defect()]))
    assert "planted:" in text and "exit 2" in text
    assert "[hard_defect] R1 schema_violation" in text
    assert "step 1" in text
    assert "/order_id  type: 4521 is not a string" in text


def test_clean_report_says_clean():
    assert "clean" in render_report(LintReport("run", []))


def test_candidates_hidden_by_default_shown_on_request():
    cand = Finding("R4", "loop", ConfidenceTier.CANDIDATE, "3 identical calls")
    report = LintReport("run", [cand])
    assert "loop" not in render_report(report)
    assert "candidate(s) hidden" in render_report(report)
    assert "loop" in render_report(report, include_candidates=True)


def test_suppressions_always_disclosed():
    report = LintReport("run", [Finding.suppressed("R1", "schema_violation", "no schema for 'x'")])
    text = render_report(report)
    assert "suppressed (1)" in text
    assert "no schema for 'x'" in text
    assert "not a clean pass" in text


def test_possible_false_positive_annotated():
    f = Finding("R3", "hallucinated_arg", ConfidenceTier.CANDIDATE, "value not in provenance")
    f.possible_false_positive = True
    text = render_report(LintReport("run", [f]), include_candidates=True)
    assert "possible false positive" in text


def test_report_leads_with_the_source_path_when_given():
    text = render_report(LintReport("run-xyz", [_defect()]), source="traces/deploy.json")
    assert text.startswith("traces/deploy.json:")
    assert "run-xyz" not in text  # the file path replaces the run id in the header


def test_report_falls_back_to_run_id_without_a_source():
    assert render_report(LintReport("run-xyz", [_defect()])).startswith("run-xyz:")


def test_a_tier_legend_is_shown_with_findings_but_not_on_a_clean_report():
    assert "tiers:" in render_report(LintReport("r", [_defect()]))
    assert "tiers:" not in render_report(LintReport("r", []))


def test_render_reports_adds_a_cross_file_summary_for_multiple_traces():
    text = render_reports(
        [LintReport("a", [_defect()]), LintReport("b", [])],
        sources=["traces/a.json", "traces/b.json"],
    )
    assert "linted 2 trace(s)" in text and "1 with a hard_defect" in text
    assert "traces/a.json:" in text and "traces/b.json:" in text


def test_render_reports_omits_the_summary_for_a_single_trace():
    text = render_reports([LintReport("solo", [_defect()])], sources=["traces/solo.json"])
    assert "linted 1 trace(s)" not in text
    assert text.startswith("traces/solo.json:")
