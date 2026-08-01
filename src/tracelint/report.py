"""Human-readable and machine-readable rendering of a lint report (spec §II.10).

The text report leads with the exit-relevant facts (how many findings, the exit code), lists each
active finding with its **exact trace location** and evidence, and always discloses suppressions
in their own section — a clean report must never hide what could not be checked. Candidates are
shown only with ``include_candidates=True`` ("candidate, not verdict": heuristics are opt-in
detail, not the headline), while ``hard_event`` / ``hard_defect`` findings always show.
"""

from __future__ import annotations

import json
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
