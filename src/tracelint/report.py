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
:root{
  --bg:#f6f7f9; --surface:#ffffff; --surface2:#f2f3f6; --fg:#14161c; --muted:#5c6473;
  --line:#e6e8ee; --accent:#5b5bd6;
  --defect:#d92d3a; --defect-bg:#fdecec; --event:#b26b00; --event-bg:#fbf1dd;
  --cand:#0e8a72; --cand-bg:#e3f5ef; --pass:#12805c; --track:#e6e8ee;
  --shadow:0 1px 2px rgba(20,22,28,.05),0 1px 3px rgba(20,22,28,.06);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0d0f13; --surface:#161922; --surface2:#1b1f2a; --fg:#e7e9ee; --muted:#98a1b3;
    --line:#242938; --accent:#9b9bf0;
    --defect:#ff7a82; --defect-bg:#2a1417; --event:#f4b559; --event-bg:#2a2113;
    --cand:#4fd4b2; --cand-bg:#0f2620; --pass:#4fd4b2;
    --track:#242938; --shadow:none;
  }
}
*{box-sizing:border-box;} html{-webkit-text-size-adjust:100%;} body{margin:0;}
.wrap{max-width:1000px;margin:0 auto;padding:2.5rem 1.25rem 5rem;
  font:15px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
    Helvetica,Arial,sans-serif;
  background:var(--bg);color:var(--fg);}
a{color:var(--accent);text-decoration:none;} a:hover{text-decoration:underline;}
.hero{margin-bottom:2rem;}
.wordmark{display:inline-flex;align-items:center;gap:.55rem;font-weight:800;font-size:1.7rem;
  letter-spacing:-.02em;margin:0;}
.dot{width:.7rem;height:.7rem;border-radius:50%;background:var(--accent);
  box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 20%,transparent);}
.tagline{color:var(--muted);margin:.5rem 0 0;max-width:60ch;}
.meta{display:flex;flex-wrap:wrap;gap:.4rem .5rem;margin-top:.9rem;}
.pill{font-size:.78rem;background:var(--surface);border:1px solid var(--line);color:var(--muted);
  padding:.2rem .6rem;border-radius:999px;}
.pill code{background:none;padding:0;color:var(--fg);}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:.75rem;margin:1.75rem 0;}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:1rem 1.1rem;
  box-shadow:var(--shadow);}
.tile .n{font-size:1.7rem;font-weight:800;letter-spacing:-.02em;line-height:1.1;}
.tile .l{color:var(--muted);font-size:.82rem;margin-top:.2rem;}
.tile .n.good{color:var(--pass);} .tile .n.bad{color:var(--defect);}
h2{font-size:1.05rem;font-weight:700;letter-spacing:-.01em;margin:2.5rem 0 .35rem;}
.section-note{color:var(--muted);font-size:.88rem;margin:0 0 1rem;}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  box-shadow:var(--shadow);overflow:hidden;}
table{width:100%;border-collapse:collapse;font-size:.9rem;}
th,td{text-align:left;padding:.7rem .9rem;border-bottom:1px solid var(--line);vertical-align:top;}
tr:last-child td{border-bottom:none;}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;
  background:var(--surface2);}
tbody tr:hover{background:color-mix(in srgb,var(--accent) 4%,transparent);}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
code{background:var(--surface2);padding:.08rem .38rem;border-radius:5px;font-size:.85em;}
.mono{font-size:.82rem;color:var(--muted);}
.kind{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;margin-top:.15rem;}
.chip{display:inline-flex;align-items:center;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.75rem;font-weight:700;padding:.12rem .5rem;border-radius:6px;
  white-space:nowrap;line-height:1.5;}
.chip.hard_defect{color:var(--defect);background:var(--defect-bg);}
.chip.hard_event{color:var(--event);background:var(--event-bg);}
.chip.candidate{color:var(--cand);background:var(--cand-bg);}
.chips{display:flex;flex-wrap:wrap;gap:.3rem;align-items:center;}
.supp{color:var(--muted);font-size:.78rem;}
.clean{color:var(--pass);font-weight:700;}
.verdict{font-weight:800;font-size:.78rem;letter-spacing:.03em;}
.verdict.ok{color:var(--pass);} .verdict.no{color:var(--defect);}
.legend{display:flex;flex-wrap:wrap;gap:1rem;margin:.9rem 0 0;font-size:.82rem;color:var(--muted);}
.legend .k{display:inline-flex;align-items:center;gap:.4rem;}
.swatch{width:.7rem;height:.7rem;border-radius:3px;display:inline-block;}
.sc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-top:.5rem;}
.sc{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:1.1rem 1.2rem;
  box-shadow:var(--shadow);}
.sc h3{margin:0 0 .1rem;font-size:.98rem;}
.sc .mode{color:var(--muted);font-size:.8rem;margin-bottom:.8rem;}
.row{display:grid;grid-template-columns:5.5rem 1fr auto;gap:.6rem;
  align-items:center;margin:.55rem 0;}
.row .f{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.8rem;color:var(--muted);}
.bar{height:8px;border-radius:99px;background:var(--track);overflow:hidden;}
.bar>span{display:block;height:100%;border-radius:99px;}
.row .v{font-size:.82rem;font-weight:700;white-space:nowrap;}
.ci{color:var(--muted);font-size:.72rem;font-weight:400;}
.footer{margin-top:3rem;padding-top:1.25rem;border-top:1px solid var(--line);color:var(--muted);
  font-size:.82rem;}
"""

_TIER_META = {
    ConfidenceTier.HARD_DEFECT: (
        "hard_defect",
        "--defect",
        "structurally-provable defect; fails CI",
    ),
    ConfidenceTier.HARD_EVENT: ("hard_event", "--event", "a real event (e.g. a tool error)"),
    ConfidenceTier.CANDIDATE: (
        "candidate",
        "--cand",
        "heuristic signal for review; never fails CI",
    ),
}


def _chip(tier: ConfidenceTier, text: str) -> str:
    cls = _TIER_META[tier][0]
    return f'<span class="chip {cls}">{_esc(text)}</span>'


def _findings_cell(report: LintReport) -> str:
    if not report.active_findings:
        chips = ['<span class="clean">clean</span>']
    else:
        chips = [_chip(f.tier, f.rule) for f in report.active_findings]
    if report.suppressions:
        chips.append(f'<span class="supp">+{len(report.suppressions)} suppressed</span>')
    return f'<div class="chips">{"".join(chips)}</div>'


def _legend() -> str:
    keys = "".join(
        f'<span class="k"><span class="swatch" style="background:var({var})"></span>'
        f"<b>{label}</b> — {_esc(desc)}</span>"
        for label, var, desc in _TIER_META.values()
    )
    return f'<div class="legend">{keys}</div>'


def render_html(
    *,
    title: str = "tracelint report",
    reports: list[LintReport] | None = None,
    validation: list[tuple] | None = None,
    scorecards: list | None = None,
) -> str:
    """Render a single self-contained HTML page (inline CSS, no scripts, no external resources)."""
    body: list[str] = [_hero(title, validation, scorecards, reports)]
    if validation is not None:
        body.append(_validation_section(validation))
    if scorecards:
        body.append(_scorecards_section(scorecards))
    if reports is not None:
        body.append(_reports_section(reports))
    body.append(
        '<div class="footer">Generated by <code>tracelint</code> — a deterministic, judge-free '
        "static analyzer for tool-calling agent traces. "
        "<code>pip install tracelint</code> &middot; "
        '<a href="https://github.com/AshwinUgale/tracelint">github.com/AshwinUgale/tracelint</a></div>'
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class='wrap'>{''.join(body)}</div></body></html>"
    )


def _hero(title, validation, scorecards, reports) -> str:
    tiles = []
    if validation is not None:
        passed = sum(1 for *_r, ok in validation if ok)
        total = len(validation)
        planted = sum(1 for c, *_r in validation if getattr(c, "kind", "") == "planted")
        cls = "good" if passed == total else "bad"
        tiles.append(_tile(f"{passed}/{total}", "validation cases pass", cls))
        tiles.append(_tile(str(planted), "planted defects caught"))
    if scorecards:
        for sc in scorecards:
            if not sc.baseline_ok or not sc.results:
                continue
            avg = sum(r.rate for r in sc.results) / len(sc.results)
            good = "good" if avg >= 0.5 else "bad"
            label = (
                "robust agent recovery"
                if "robust" in sc.task
                else ("buggy agent recovery" if "buggy" in sc.task else f"{_esc(sc.task)} recovery")
            )
            tiles.append(_tile(f"{avg * 100:.0f}%", label, good))
    if reports is not None and validation is None:
        n = sum(len(r.active_findings) for r in reports)
        tiles.append(_tile(str(len(reports)), "traces linted"))
        tiles.append(_tile(str(n), "findings"))
    tiles_html = f'<div class="tiles">{"".join(tiles)}</div>' if tiles else ""
    return (
        '<div class="hero"><h1 class="wordmark"><span class="dot"></span>'
        f"{_esc(title)}</h1>"
        '<p class="tagline">Deterministic, judge-free analysis of tool-calling agent traces. '
        "Hard defects fail CI; events and candidates are shown for review, never asserted — "
        "no second model ever judges the trace.</p>"
        '<div class="meta"><span class="pill"><code>pip install tracelint</code></span>'
        '<span class="pill">judge-free</span><span class="pill">deterministic</span>'
        "</div></div>" + tiles_html
    )


def _tile(number: str, label: str, cls: str = "") -> str:
    return (
        f'<div class="tile"><div class="n {cls}">{_esc(number)}</div>'
        f'<div class="l">{_esc(label)}</div></div>'
    )


def _validation_section(validation: list[tuple]) -> str:
    passed = sum(1 for *_rest, ok in validation if ok)
    rows = []
    for case, report, ok in validation:
        verdict = (
            '<span class="verdict ok">PASS</span>' if ok else '<span class="verdict no">FAIL</span>'
        )
        rows.append(
            f"<tr><td>{verdict}</td>"
            f"<td><code>{_esc(case.name)}</code><div class='kind'>{_esc(case.kind)}</div></td>"
            f"<td>{_esc(case.description)}</td>"
            f"<td>{_esc(case.expectation)}</td>"
            f"<td>{_findings_cell(report)}</td></tr>"
        )
    return (
        f"<h2>Validation suite &mdash; {passed}/{len(validation)} behaved as expected</h2>"
        "<p class='section-note'>One planted instance of each defect, clean controls, and "
        "legitimate-but-suspicious cases (a real retry, a value transform, a generated key). "
        "Each case asserts an expected outcome; the linter must match it.</p>"
        "<div class='card'><table><thead><tr><th>Result</th><th>Case</th><th>Scenario</th>"
        f"<th>Expected</th><th>Findings</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        + _legend()
    )


def _rate_color(rate: float) -> str:
    return "var(--pass)" if rate >= 0.5 else "var(--defect)"


def _scorecards_section(scorecards: list) -> str:
    cards = []
    for sc in scorecards:
        head = f"<h3><code>{_esc(sc.task)}</code></h3>"
        if not sc.baseline_ok:
            cards.append(f"<div class='sc'>{head}<p class='mode'>{_esc(sc.note)}</p></div>")
            continue
        mode = f"{sc.mode} recovery"
        if sc.mode == "behavioral":
            mode += " &mdash; weaker (no oracle)"
        rows = []
        for r in sc.results:
            lo, hi = r.ci
            rows.append(
                f"<div class='row'><span class='f'>{_esc(r.fault.value)}</span>"
                f"<span class='bar'><span style='width:{r.rate * 100:.0f}%;"
                f"background:{_rate_color(r.rate)}'></span></span>"
                f"<span class='v'>{r.recovered}/{r.total} &middot; {r.rate * 100:.0f}%"
                f"<br><span class='ci'>CI {lo:.2f}&ndash;{hi:.2f}</span></span></div>"
            )
        cards.append(
            f"<div class='sc'>{head}<div class='mode'>mode: {mode}</div>{''.join(rows)}</div>"
        )
    return (
        "<h2>Recovery scorecard</h2>"
        "<p class='section-note'>Each agent is run against injected faults; recovery is scored "
        "against a deterministic success oracle, with a 95% Wilson CI.</p>"
        f"<div class='sc-grid'>{''.join(cards)}</div>"
    )


def _reports_section(reports: list[LintReport]) -> str:
    blocks = ["<h2>Traces</h2>"]
    for report in reports:
        exit_note = f"exit {report.exit_code}"
        rid = _esc(report.run_id)
        blocks.append(
            f"<h3 style='margin:1.5rem 0 .5rem;font-size:.98rem'><code>{rid}</code> "
            f"<span class='supp'>{exit_note}</span></h3>"
        )
        if not report.active_findings and not report.suppressions:
            blocks.append("<p class='clean'>clean &mdash; no findings.</p>")
            continue
        rows = []
        for f in report.active_findings:
            loc = ", ".join(str(i) for i in f.step_indices) or "&mdash;"
            rows.append(
                f"<tr><td>{_chip(f.tier, f.tier.value)}</td>"
                f"<td><code>{_esc(f.rule)}</code> {_esc(f.finding_type)}</td>"
                f"<td>{_esc(f.summary)}</td><td class='mono'>{loc}</td></tr>"
            )
        table = (
            "<div class='card'><table><thead><tr><th>Tier</th><th>Rule</th><th>Finding</th>"
            f"<th>Steps</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
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
                "<p class='section-note' style='margin-top:.6rem'>Suppressed "
                f"(not checked &mdash; not a clean pass):</p><ul class='supp'>{items}</ul>"
            )
        blocks.append(table + supp)
    return "".join(blocks)


def write_html(path: str | Path, html: str) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
