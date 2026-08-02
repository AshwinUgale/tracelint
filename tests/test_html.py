"""Phase 7 — the self-contained HTML report."""

from __future__ import annotations

from tracelint import ConfidenceTier, Finding, LintReport, render_html, write_html


def _report():
    return LintReport(
        "run-1",
        [
            Finding(
                "R1", "schema_violation", ConfidenceTier.HARD_DEFECT,
                "bad call", evidence={"step_indices": [1]},
            ),
            Finding.suppressed("R3", "hallucinated_arg", "no schema"),
        ],
    )


def test_html_is_self_contained():
    html = render_html(title="t", reports=[_report()])
    assert html.startswith("<!doctype html>")
    # No external resources are LOADED (a plain hyperlink to the repo is fine).
    lower = html.lower()
    assert "<script" not in lower
    assert "<link " not in lower
    assert "src=" not in lower
    assert "@import" not in lower
    assert "url(http" not in lower
    assert "<style>" in html  # CSS is inlined


def test_html_shows_findings_and_suppressions():
    html = render_html(title="t", reports=[_report()])
    assert "run-1" in html and "hard_defect" in html
    assert "schema_violation" in html
    assert "suppressed" in html.lower() and "no schema" in html


def test_html_escapes_content():
    report = LintReport(
        "run", [Finding("R1", "x", ConfidenceTier.HARD_DEFECT, "<script>alert(1)</script>")]
    )
    html = render_html(reports=[report])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_theme_aware():
    html = render_html(reports=[_report()])
    assert "prefers-color-scheme: dark" in html


def test_write_html_creates_parent_dirs(tmp_path):
    out = tmp_path / "nested" / "report.html"
    write_html(out, render_html(reports=[_report()]))
    assert out.exists() and out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_html_renders_validation_and_scorecards():
    from tracelint import FaultType, run_scorecard
    from tracelint.agent import build_recovery_task
    from tracelint.rules import default_rules, lint_trace
    from tracelint.validation import validation_cases

    cases = validation_cases()[:3]
    results = [(c, lint_trace(c.trace, default_rules(), c.registry), True) for c in cases]
    sc = run_scorecard(build_recovery_task(), [FaultType.ERROR])
    html = render_html(title="demo", validation=results, scorecards=[sc])
    assert "Validation suite" in html and "Recovery scorecard" in html
    assert "Wilson CI" in html
