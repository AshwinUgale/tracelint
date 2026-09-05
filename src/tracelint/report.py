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
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult


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

    if report.coverage:
        lines.append("  verification coverage (evaluatable / total):")
        for c in report.coverage:
            lines.append(f"    {c.rule}  {c.evaluatable}/{c.total} {c.unit}")

    if not active:
        if report.suppressions:
            lines.append(
                "  no structural issues found — but some rules were suppressed above "
                "(not checked); run `tracelint init` to generate a tools.json and enable them."
            )
        else:
            lines.append("  clean — no structural issues found.")
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
*{box-sizing:border-box;} html{-webkit-text-size-adjust:100%;}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--fg);
  font:15px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
    Helvetica,Arial,sans-serif;}
.wrap{max-width:1000px;margin:0 auto;padding:2.5rem 1.25rem 5rem;}
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
.explain{display:grid;grid-template-columns:1.35fr 1fr;gap:1rem;margin:.3rem 0 0;}
@media(max-width:760px){.explain{grid-template-columns:1fr;}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:1.1rem 1.2rem;}
.panel h3{margin:0 0 .55rem;font-size:.9rem;}
.rule{display:grid;grid-template-columns:2.7rem 1fr auto;gap:.15rem .6rem;
  padding:.5rem 0;border-top:1px solid var(--line);align-items:baseline;}
.rule:first-of-type{border-top:none;padding-top:.1rem;}
.rule .rid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-weight:700;font-size:.82rem;}
.rule .rd{font-size:.86rem;} .rule .rd b{font-weight:650;}
.rule .rd span{color:var(--muted);}
.tierrow{padding:.5rem 0;border-top:1px solid var(--line);}
.tierrow:first-of-type{border-top:none;padding-top:.1rem;}
.tierrow .td{color:var(--muted);font-size:.85rem;margin-top:.25rem;}
.tl{list-style:none;padding:0;margin:.15rem 0 0;}
.tl li{display:grid;grid-template-columns:5.2rem 1fr;gap:.7rem;padding:.32rem 0;
  align-items:baseline;border-top:1px dashed var(--line);}
.tl li:first-child{border-top:none;}
.tl .lm{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.72rem;color:var(--muted);
  text-transform:uppercase;letter-spacing:.03em;}
.tl .lc{font-size:.88rem;} .tl .lc.err{color:var(--defect);} .tl .lc.good{color:var(--cand);}
.task{color:var(--muted);font-size:.85rem;margin:0 0 .7rem;}
.task b{color:var(--fg);font-weight:600;}
/* --- linted trace view: step timeline + findings --- */
.trace-view{display:grid;grid-template-columns:1.15fr .85fr;gap:1rem;margin:.2rem 0 0;
  align-items:start;}
@media(max-width:860px){.trace-view{grid-template-columns:1fr;}}
.timeline{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  box-shadow:var(--shadow);overflow:hidden;}
.tstep{display:grid;grid-template-columns:1.7rem 4.6rem 1fr auto;gap:.55rem;align-items:baseline;
  padding:.55rem .9rem;border-bottom:1px solid var(--line);font-size:.86rem;}
.tstep:last-child{border-bottom:none;}
.tstep .ti{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.76rem;font-variant-numeric:tabular-nums;}
.tstep .tk{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.72rem;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap;}
.tstep .tc{color:var(--fg);word-break:break-word;}
.tstep .tc.err{color:var(--defect);} .tstep .tc.good{color:var(--pass);}
.tstep.hit{box-shadow:inset 3px 0 0 var(--defect);}
.tstep.hit.evt{box-shadow:inset 3px 0 0 var(--event);}
.tstep.hit.cand{box-shadow:inset 3px 0 0 var(--cand);}
.tfind{justify-self:end;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.68rem;
  font-weight:700;padding:.05rem .4rem;border-radius:5px;}
.tfind.def{color:var(--defect);background:var(--defect-bg);}
.tfind.evt{color:var(--event);background:var(--event-bg);}
.tfind.cand{color:var(--cand);background:var(--cand-bg);}
.rfindings{display:flex;flex-direction:column;gap:.6rem;}
.rfind{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--defect);
  border-radius:12px;padding:.75rem .85rem;box-shadow:var(--shadow);}
.rfind.evt{border-left-color:var(--event);} .rfind.cand{border-left-color:var(--cand);}
.rf-top{display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;font-size:.82rem;margin-bottom:.35rem;}
.rf-steps{color:var(--muted);margin-left:auto;font-size:.75rem;white-space:nowrap;}
.rf-sum{margin:.15rem 0 .45rem;font-size:.9rem;color:var(--fg);line-height:1.5;}
.rf-ev{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.75rem;color:var(--muted);
  border-top:1px solid var(--line);padding-top:.45rem;line-height:1.5;}
.rf-ev b{color:var(--fg);}
.cov{margin:.9rem 0 0;background:var(--surface2);border:1px solid var(--line);border-radius:12px;
  padding:.7rem .9rem;}
.cov .covh{font-size:.74rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);
  margin-bottom:.5rem;}
.covline{display:grid;grid-template-columns:4rem 1fr auto;gap:.6rem;align-items:center;
  font-size:.8rem;margin:.28rem 0;}
.covline .cvb{height:6px;border-radius:99px;background:var(--track);overflow:hidden;}
.covline .cvb i{display:block;height:100%;background:var(--pass);}
.covline .cvv{color:var(--muted);font-size:.76rem;white-space:nowrap;}
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


_RULES = (
    (
        "R1",
        "Schema violation",
        "a tool call's arguments don't match the tool's JSON Schema",
        ConfidenceTier.HARD_DEFECT,
    ),
    (
        "R2a",
        "Tool error",
        "a tool returned a structured error (e.g. HTTP status >= 400)",
        ConfidenceTier.HARD_EVENT,
    ),
    (
        "R2b",
        "Error consumed",
        "a value from an errored result is reused by a later side-effecting call",
        ConfidenceTier.HARD_DEFECT,
    ),
    (
        "R3",
        "Hallucinated argument",
        "an argument value isn't derivable from anything in the trace",
        ConfidenceTier.CANDIDATE,
    ),
    (
        "R4",
        "Loop",
        "the same call repeats with no progress (retries and polls excluded)",
        ConfidenceTier.CANDIDATE,
    ),
    (
        "R5",
        "Redundant call",
        "an identical call and result with no state change between",
        ConfidenceTier.CANDIDATE,
    ),
    (
        "R6",
        "Malformed arguments",
        "the emitted tool-call arguments aren't valid JSON",
        ConfidenceTier.HARD_DEFECT,
    ),
    (
        "R7",
        "Unknown tool",
        "a tool was called that isn't in the declared toolset",
        ConfidenceTier.CANDIDATE,
    ),
)

_TIERS = (
    (ConfidenceTier.HARD_DEFECT, "Structurally provable from the trace itself. Fails CI (exit 2)."),
    (
        ConfidenceTier.HARD_EVENT,
        "A real, observed event (e.g. a tool returned an error). Reported; does not fail CI.",
    ),
    (
        ConfidenceTier.CANDIDATE,
        "A heuristic signal shown with its evidence for review. Never fails CI on its own.",
    ),
)


def _explainer() -> str:
    rules = "".join(
        f"<div class='rule'><span class='rid'>{_esc(rid)}</span>"
        f"<span class='rd'><b>{_esc(name)}</b> <span>&mdash; {_esc(desc)}</span></span>"
        f"{_chip(tier, tier.value)}</div>"
        for rid, name, desc, tier in _RULES
    )
    tiers = "".join(
        f"<div class='tierrow'>{_chip(tier, tier.value)}<div class='td'>{_esc(desc)}</div></div>"
        for tier, desc in _TIERS
    )
    return (
        "<h2>What tracelint checks</h2>"
        "<p class='section-note'>tracelint reads a finished agent trace and flags "
        "<b>structural</b> defects, each with the exact step as evidence &mdash; no second model "
        "judges the trace. Every finding carries a confidence tier that decides whether it fails "
        "CI.</p>"
        "<div class='explain'>"
        f"<div class='panel'><h3>Eight deterministic rules</h3>{rules}</div>"
        f"<div class='panel'><h3>Three confidence tiers</h3>{tiers}"
        "<p class='td' style='margin-top:.7rem'>In the tables below, the <b>Findings</b> column "
        "shows the rules that fired (coloured chips) plus how many checks were <i>suppressed</i> "
        "because the trace lacked a field they needed &mdash; a suppression is never a clean "
        "pass.</p></div>"
        "</div>"
    )


def _step_line(step: object) -> str:
    if isinstance(step, Message):
        return (
            f"<li><span class='lm'>{_esc(step.role.value)}</span>"
            f"<span class='lc'>{_esc(step.content or '')}</span></li>"
        )
    if isinstance(step, ToolCall):
        args = ", ".join(f"{k}={v!r}" for k, v in (step.args or {}).items())
        return (
            "<li><span class='lm'>tool &rarr;</span>"
            f"<span class='lc'><code>{_esc(step.name)}</code>({_esc(args)})</span></li>"
        )
    if isinstance(step, ToolResult):
        is_err = step.status is ResultStatus.ERROR
        cls = "err" if is_err else ("good" if step.status is ResultStatus.OK else "")
        detail = step.error or step.content
        return (
            f"<li><span class='lm'>{'error' if is_err else 'result'}</span>"
            f"<span class='lc {cls}'>{_esc(str(detail))}</span></li>"
        )
    return ""


def _worked_section(examples: list[tuple]) -> str:
    blocks = [
        "<h2>Worked example &mdash; a real agent run</h2>",
        "<p class='section-note'>One agent trajectory linted end to end: the steps the agent "
        "actually took, then exactly what tracelint found and the step it points to &mdash; the "
        "same output as <code>tracelint check your-trace.json</code>.</p>",
    ]
    for trace, report in examples:
        first_user = next(
            (s.content for s in trace.steps if isinstance(s, Message) and s.role is Role.USER),
            "",
        )
        steps = "".join(_step_line(s) for s in trace.steps)
        blocks.append(
            f"<div class='panel'><p class='task'>Task: <b>{_esc(first_user)}</b> &middot; "
            f"<span class='mono'>exit {report.exit_code}</span></p>"
            f"<ol class='tl'>{steps}</ol></div>"
        )
        rows = "".join(
            f"<tr><td>{_chip(f.tier, f.tier.value)}</td>"
            f"<td><code>{_esc(f.rule)}</code> {_esc(f.finding_type)}</td>"
            f"<td>{_esc(f.summary)}</td>"
            f"<td class='mono'>{', '.join(str(i) for i in f.step_indices) or '&mdash;'}</td></tr>"
            for f in report.active_findings
        )
        if rows:
            blocks.append(
                "<div class='card' style='margin-top:1rem'><table><thead><tr><th>Tier</th>"
                "<th>Rule</th><th>Finding</th><th>Steps</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>"
            )
        blocks.append(_legend())
    return "".join(blocks)


def render_html(
    *,
    title: str = "tracelint report",
    reports: list[LintReport] | None = None,
    traces: list | None = None,
    validation: list[tuple] | None = None,
    scorecards: list | None = None,
    worked: list[tuple] | None = None,
) -> str:
    """Render a single self-contained HTML page (inline CSS, no scripts, no external resources).

    When ``traces`` is given alongside ``reports`` (same order), each linted run renders as a
    step timeline with its findings marked inline — the trace view. Without ``traces`` a compact
    findings table is used instead.
    """
    body: list[str] = [_hero(title, validation, scorecards, reports)]
    if validation is not None or worked:
        body.append(_explainer())
    if worked:
        body.append(_worked_section(worked))
    if validation is not None:
        body.append(_validation_section(validation))
    if scorecards:
        body.append(_scorecards_section(scorecards))
    if reports is not None:
        body.append(_reports_section(reports, traces))
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
        '<p class="tagline"><b>A linter for agent runs.</b> It reads the execution trace — what '
        "your agent actually did — and flags structural bugs deterministically. Hard defects "
        "fail CI; events and candidates are shown for review, never asserted — no second model "
        "ever judges the trace.</p>"
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


_TIER_SHORT = {
    ConfidenceTier.HARD_DEFECT: "def",
    ConfidenceTier.HARD_EVENT: "evt",
    ConfidenceTier.CANDIDATE: "cand",
}
_TIER_ORDER = {
    ConfidenceTier.HARD_DEFECT: 0,
    ConfidenceTier.HARD_EVENT: 1,
    ConfidenceTier.CANDIDATE: 2,
}


def _evidence_snippet(f: Finding) -> str:
    """A short key=value line from a finding's evidence (scalars only; steps shown separately)."""
    parts = []
    for k, v in (f.evidence or {}).items():
        if k == "step_indices" or not isinstance(v, (str, int, float, bool)):
            continue
        s = str(v)
        if len(s) > 80:
            s = s[:77] + "…"
        parts.append(f"{_esc(k)}=<b>{_esc(s)}</b>")
        if len(parts) == 3:
            break
    return " · ".join(parts)


def _tstep_row(step: object, hit_short: str, badge: str) -> str:
    """One step in the linted-trace timeline; ``hit_short`` colours it if a finding touches it."""
    if isinstance(step, Message):
        kind, body = step.role.value, f"<span class='tc'>{_esc(step.content or '')}</span>"
    elif isinstance(step, ToolCall):
        args = ", ".join(f"{k}={v!r}" for k, v in (step.args or {}).items())
        kind = "tool →"
        body = f"<span class='tc'><code>{_esc(step.name)}</code>({_esc(args)})</span>"
    elif isinstance(step, ToolResult):
        is_err = step.status is ResultStatus.ERROR
        cls = "err" if is_err else ("good" if step.status is ResultStatus.OK else "")
        kind = "error" if is_err else "result"
        detail = step.error if step.error else step.content
        body = f"<span class='tc {cls}'>{_esc(str(detail))}</span>"
    else:
        return ""
    hit = f" hit {hit_short}" if hit_short else ""
    return (
        f"<div class='tstep{hit}'><span class='ti'>{step.index}</span>"
        f"<span class='tk'>{_esc(kind)}</span>{body}{badge or '<span></span>'}</div>"
    )


def _trace_view(trace, report: LintReport) -> str:
    """Render a linted run as a step timeline (findings marked inline) + finding cards."""
    by_step: dict[int, list[Finding]] = {}
    for f in report.active_findings:
        for i in f.step_indices:
            by_step.setdefault(i, []).append(f)

    rows = []
    for step in trace.steps:
        hits = by_step.get(step.index, [])
        short, badge = "", ""
        if hits:
            worst = min(hits, key=lambda f: _TIER_ORDER[f.tier])
            short = _TIER_SHORT[worst.tier]
            badge = f"<span class='tfind {short}'>{_esc(worst.rule)}</span>"
        rows.append(_tstep_row(step, short, badge))
    timeline = f"<div class='timeline'>{''.join(rows)}</div>"

    if report.active_findings:
        cards = []
        for f in report.active_findings:
            short = _TIER_SHORT[f.tier]
            steps = ", ".join(str(i) for i in f.step_indices) or "&mdash;"
            ev = _evidence_snippet(f)
            cards.append(
                f"<div class='rfind {short}'>"
                f"<div class='rf-top'>{_chip(f.tier, f.tier.value)}<code>{_esc(f.rule)}</code> "
                f"{_esc(f.finding_type)}<span class='rf-steps'>steps {steps}</span></div>"
                f"<p class='rf-sum'>{_esc(f.summary)}</p>"
                + (f"<div class='rf-ev'>{ev}</div>" if ev else "")
                + "</div>"
            )
        findings = f"<div class='rfindings'>{''.join(cards)}</div>"
    else:
        findings = "<div class='rfindings'><p class='clean'>clean &mdash; no findings.</p></div>"
    return f"<div class='trace-view'>{timeline}{findings}</div>"


def _coverage_block(report: LintReport) -> str:
    if not report.coverage:
        return ""
    lines = []
    for c in report.coverage:
        pct = 100 if c.total == 0 else round(c.evaluatable / c.total * 100)
        lines.append(
            f"<div class='covline'><span class='mono'><code>{_esc(c.rule)}</code></span>"
            f"<span class='cvb'><i style='width:{pct}%'></i></span>"
            f"<span class='cvv'>{c.evaluatable}/{c.total} {_esc(c.unit)}</span></div>"
        )
    return f"<div class='cov'><div class='covh'>verification coverage</div>{''.join(lines)}</div>"


def _reports_section(reports: list[LintReport], traces: list | None = None) -> str:
    blocks = [
        "<h2>Traces</h2>",
        "<p class='section-note'>Each linted run: the steps the agent took, and the findings "
        "tracelint returned &mdash; the same output as <code>tracelint check</code>.</p>",
    ]
    for i, report in enumerate(reports):
        trace = traces[i] if traces is not None and i < len(traces) else None
        blocks.append(
            "<h3 style='margin:1.6rem 0 .55rem;font-size:.98rem'>"
            f"<code>{_esc(report.run_id)}</code> "
            f"<span class='supp'>exit {report.exit_code}</span></h3>"
        )
        if trace is not None:
            blocks.append(_trace_view(trace, report))
        elif report.active_findings:
            rows = "".join(
                f"<tr><td>{_chip(f.tier, f.tier.value)}</td>"
                f"<td><code>{_esc(f.rule)}</code> {_esc(f.finding_type)}</td>"
                f"<td>{_esc(f.summary)}</td><td class='mono'>"
                f"{', '.join(str(i) for i in f.step_indices) or '&mdash;'}</td></tr>"
                for f in report.active_findings
            )
            blocks.append(
                "<div class='card'><table><thead><tr><th>Tier</th><th>Rule</th><th>Finding</th>"
                f"<th>Steps</th></tr></thead><tbody>{rows}</tbody></table></div>"
            )
        elif not report.suppressions:
            blocks.append("<p class='clean'>clean &mdash; no findings.</p>")

        blocks.append(_coverage_block(report))
        if report.suppressions:
            items = "".join(
                f"<li><code>{_esc(s.rule)}</code>: {_esc(s.suppressed_reason or '')}</li>"
                for s in report.suppressions
            )
            blocks.append(
                "<p class='section-note' style='margin-top:.6rem'>Suppressed "
                f"(not checked &mdash; not a clean pass):</p><ul class='supp'>{items}</ul>"
            )
    blocks.append(_legend())
    return "".join(blocks)


def write_html(path: str | Path, html: str) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
