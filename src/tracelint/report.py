"""Human-readable and machine-readable rendering of a lint report (spec §II.10).

The text report leads with the exit-relevant facts (how many findings, the exit code), lists each
active finding with its **exact trace location** and evidence, and always discloses suppressions
in their own section — a clean report must never hide what could not be checked. Candidates are
shown only with ``include_candidates=True`` ("candidate, not verdict": heuristics are opt-in
detail, not the headline), while ``hard_event`` / ``hard_defect`` findings always show.
"""

from __future__ import annotations

import json
from html import escape as _esc
from pathlib import Path
from typing import Any

from tracelint.findings import ConfidenceTier, Finding, LintReport


def _location(finding: Finding) -> str:
    idx = finding.step_indices
    return "step " + ",".join(str(i) for i in idx) if idx else "no step"


def render_report(report: LintReport, *, include_candidates: bool = False) -> str:
    """Render one :class:`LintReport` as text."""
    active = report.active_findings
    shown = [f for f in active if include_candidates or f.tier is not ConfidenceTier.CANDIDATE]
    lines = [f"{report.run_id}: {len(active)} finding(s), exit {report.exit_code}"]

    for f in shown:
        lines.append(f"  [{f.tier.value}] {f.rule} {f.finding_type}  ({_location(f)})")
        lines.append(f"    {f.summary}")
        for err in f.evidence.get("errors", []):
            lines.append(f"      {err['path']}  {err['keyword']}: {err['message']}")
        if f.possible_false_positive:
            lines.append("    (possible false positive — review the evidence)")

    hidden = len(active) - len(shown)
    if hidden:
        lines.append(f"  ({hidden} candidate(s) hidden; pass --include-candidates to show)")

    if report.suppressions:
        lines.append(f"  suppressed ({len(report.suppressions)}) — not checked, not a clean pass:")
        for s in report.suppressions:
            lines.append(f"    {s.rule} {s.finding_type}: {s.suppressed_reason}")

    if not active and not report.suppressions:
        lines.append("  clean — no findings.")
    return "\n".join(lines)


def render_reports(reports: list[LintReport], *, include_candidates: bool = False) -> str:
    """Render several reports with a one-line summary header."""
    n_defect = sum(1 for r in reports if r.has_hard_defect)
    header = (
        f"linted {len(reports)} trace(s); "
        f"{n_defect} with a hard_defect; overall exit "
        f"{max((r.exit_code for r in reports), default=0)}"
    )
    body = "\n\n".join(render_report(r, include_candidates=include_candidates) for r in reports)
    return f"{header}\n\n{body}" if body else header


def reports_to_dict(reports: list[LintReport]) -> dict[str, Any]:
    return {
        "overall_exit": max((r.exit_code for r in reports), default=0),
        "reports": [r.to_dict() for r in reports],
    }


def write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- Self-contained HTML report --------------------------------------------------------

_CSS = """
:root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e3e3e3; --card:#f7f7f8;
  --hard_defect:#c0392b; --hard_event:#c77700; --candidate:#1f7a5c; --suppressed:#888; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15171a; --fg:#e6e6e6; --muted:#9aa0a6; --line:#2c2f34; --card:#1d2024;
    --hard_defect:#ff6b5e; --hard_event:#f0a83c; --candidate:#4fd0a0; --suppressed:#9aa0a6; } }
* { box-sizing:border-box; } body { margin:0; }
.wrap { max-width:960px; margin:0 auto; padding:2rem 1.25rem 4rem;
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg); color:var(--fg); }
h1 { font-size:1.6rem; margin:0 0 .25rem; } h2 { font-size:1.15rem; margin:2rem 0 .75rem;
  border-bottom:1px solid var(--line); padding-bottom:.35rem; }
.sub { color:var(--muted); margin:0 0 1rem; }
table { width:100%; border-collapse:collapse; font-size:.92rem; }
th,td { text-align:left; padding:.5rem .6rem; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { color:var(--muted); font-weight:600; }
code { background:var(--card); padding:.05rem .35rem; border-radius:4px; font-size:.85em; }
.badge { display:inline-block; padding:.1rem .5rem; border-radius:999px; font-size:.72rem;
  font-weight:700; color:#fff; white-space:nowrap; }
.pass { color:var(--candidate); font-weight:700; } .fail { color:var(--hard_defect);
  font-weight:700; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.82rem;
  color:var(--muted); }
.note { color:var(--muted); font-size:.85rem; }
"""

_TIER_LABEL = {
    ConfidenceTier.HARD_DEFECT: ("hard_defect", "--hard_defect"),
    ConfidenceTier.HARD_EVENT: ("hard_event", "--hard_event"),
    ConfidenceTier.CANDIDATE: ("candidate", "--candidate"),
}


def _badge(tier: ConfidenceTier) -> str:
    label, var = _TIER_LABEL[tier]
    return f'<span class="badge" style="background:var({var})">{label}</span>'


def _findings_summary(report: LintReport) -> str:
    if not report.active_findings:
        parts = ["<span class='pass'>clean</span>"]
    else:
        parts = [_badge(f.tier) + f" {_esc(f.rule)}" for f in report.active_findings]
    if report.suppressions:
        parts.append(f"<span class='note'>+{len(report.suppressions)} suppressed</span>")
    return " ".join(parts)


def render_html(
    *,
    title: str = "tracelint report",
    reports: list[LintReport] | None = None,
    validation: list[tuple] | None = None,
    scorecards: list | None = None,
) -> str:
    """Render a single self-contained HTML page (inline CSS, no scripts, no external resources)."""
    body: list[str] = [f"<h1>{_esc(title)}</h1>"]
    body.append(
        "<p class='sub'>Deterministic, judge-free analysis of tool-calling agent traces. "
        "Hard defects fail CI; events and candidates are shown for review, never asserted.</p>"
    )

    if validation is not None:
        body.append(_validation_section(validation))
    if scorecards:
        body.append(_scorecards_section(scorecards))
    if reports is not None:
        body.append(_reports_section(reports))

    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class='wrap'>{''.join(body)}</div></body></html>"
    )


def _validation_section(validation: list[tuple]) -> str:
    passed = sum(1 for *_rest, ok in validation if ok)
    rows = []
    for case, report, ok in validation:
        verdict = "<span class='pass'>PASS</span>" if ok else "<span class='fail'>FAIL</span>"
        rows.append(
            f"<tr><td>{verdict}</td><td><code>{_esc(case.name)}</code><br>"
            f"<span class='note'>{_esc(case.kind)}</span></td>"
            f"<td>{_esc(case.description)}</td>"
            f"<td>{_esc(case.expectation)}</td>"
            f"<td>{_findings_summary(report)}</td></tr>"
        )
    return (
        f"<h2>Validation suite — {passed}/{len(validation)} behaved as expected</h2>"
        "<p class='note'>One planted instance of each defect, clean controls, and "
        "legitimate-but-suspicious cases (a real retry, a value transform, a generated key).</p>"
        "<table><tr><th>Result</th><th>Case</th><th>Scenario</th><th>Expected</th>"
        f"<th>Findings</th></tr>{''.join(rows)}</table>"
    )


def _scorecards_section(scorecards: list) -> str:
    blocks = ["<h2>Recovery scorecard</h2>"]
    for sc in scorecards:
        blocks.append(f"<h3 style='margin:1rem 0 .4rem'><code>{_esc(sc.task)}</code></h3>")
        if not sc.baseline_ok:
            blocks.append(f"<p class='note'>{_esc(sc.note)}</p>")
            continue
        mode = f"{sc.mode} recovery"
        if sc.mode == "behavioral":
            mode += " (weaker — no oracle)"
        rows = []
        for r in sc.results:
            lo, hi = r.ci
            rows.append(
                f"<tr><td><code>{_esc(r.fault.value)}</code></td>"
                f"<td>{r.recovered}/{r.total}</td><td>{r.rate:.2f}</td>"
                f"<td class='mono'>[{lo:.2f}, {hi:.2f}]</td></tr>"
            )
        blocks.append(
            f"<p class='note'>mode: {_esc(mode)}</p>"
            "<table><tr><th>Fault</th><th>Recovered</th><th>Rate</th>"
            f"<th>95% Wilson CI</th></tr>{''.join(rows)}</table>"
        )
    return "".join(blocks)


def _reports_section(reports: list[LintReport]) -> str:
    blocks = ["<h2>Traces</h2>"]
    for report in reports:
        blocks.append(
            f"<h3 style='margin:1rem 0 .4rem'><code>{_esc(report.run_id)}</code> "
            f"<span class='note'>exit {report.exit_code}</span></h3>"
        )
        if not report.active_findings and not report.suppressions:
            blocks.append("<p class='pass'>clean — no findings.</p>")
            continue
        rows = []
        for f in report.active_findings:
            loc = ", ".join(str(i) for i in f.step_indices) or "—"
            rows.append(
                f"<tr><td>{_badge(f.tier)}</td><td><code>{_esc(f.rule)}</code> "
                f"{_esc(f.finding_type)}</td><td>{_esc(f.summary)}</td>"
                f"<td class='mono'>{_esc(loc)}</td></tr>"
            )
        table = (
            "<table><tr><th>Tier</th><th>Rule</th><th>Finding</th><th>Steps</th></tr>"
            f"{''.join(rows)}</table>"
            if rows
            else ""
        )
        supp = ""
        if report.suppressions:
            items = "".join(
                f"<li><code>{_esc(s.rule)}</code>: {_esc(s.suppressed_reason or '')}</li>"
                for s in report.suppressions
            )
            supp = (
                "<p class='note'>Suppressed (not checked — not a clean pass):</p>"
                f"<ul class='note'>{items}</ul>"
            )
        blocks.append(table + supp)
    return "".join(blocks)


def write_html(path: str | Path, html: str) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
