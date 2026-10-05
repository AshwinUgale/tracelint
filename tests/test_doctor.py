"""`tracelint doctor` — diagnose why rules couldn't run and name the one fix (audit 3.5).

Advisory, never a gate: it turns a wall of suppressions into the sentence that matters — your
instrumentation isn't recording tool content, you haven't declared a contract, or the rule simply
did not apply — plus a coverage summary. Built on the suppression categories (not_recorded /
needs_contract / not_applicable).
"""

from __future__ import annotations

import json

from tracelint import ToolRegistry, build_trace, lint_trace
from tracelint.cli import main
from tracelint.report import render_diagnosis
from tracelint.rules import default_rules
from tracelint.trace import ResultStatus, ToolCall, ToolResult


def _write(tmp_path, trace_dict):
    p = tmp_path / "trace.json"
    p.write_text(json.dumps(trace_dict), encoding="utf-8")
    return str(p)


def _diag(trace, registry=None, **kw):
    return render_diagnosis([lint_trace(trace, default_rules(), registry or ToolRegistry())], **kw)


def test_doctor_cli_runs_and_is_advisory(tmp_path, capsys):
    # A planted hard defect (malformed args, R6) still exits 0 — doctor diagnoses, never gates.
    trace = {
        "run_id": "r",
        "steps": [
            {"type": "message", "role": "user", "content": "charge the card"},
            {"type": "tool_call", "call_id": "c1", "name": "charge", "raw_text": "{broken"},
        ],
    }
    assert main(["doctor", _write(tmp_path, trace)]) == 0
    assert "tracelint doctor" in capsys.readouterr().out


def test_no_tools_json_is_diagnosed():
    trace = build_trace(
        "t",
        [
            ToolCall("c1", "charge", {"amt": 5}),
            ToolResult("c1", {"ok": True}, status=ResultStatus.OK),
        ],
    )
    out = _diag(trace, has_tools=False)
    assert "No tools.json" in out and "tracelint init" in out and "R1" in out


def test_not_recorded_names_the_capture_fix_for_the_format():
    reg = ToolRegistry.from_dict(
        {
            "tools": {
                "charge": {
                    "schema": {"type": "object", "properties": {"amt": {"type": "number"}}},
                    "metadata": {"side_effecting": True},
                }
            }
        }
    )
    call = ToolCall("c1", "charge", {})
    call.args_unavailable = "redacted by exporter"
    trace = build_trace("t", [call, ToolResult("c1", {"ok": True}, status=ResultStatus.OK)])
    out = _diag(trace, reg, fmt="openinference", has_tools=True)
    assert "not recorded" in out.lower()
    assert "input.value" in out and "openinference.md" in out


def test_coverage_is_aggregated_across_traces():
    reg = ToolRegistry()

    def _tr(name):
        return build_trace(
            name,
            [
                ToolCall("c1", "charge", {"a": 1}),
                ToolResult("c1", {"ok": True}, status=ResultStatus.OK),
            ],
        )

    reps = [lint_trace(_tr("a"), default_rules(), reg), lint_trace(_tr("b"), default_rules(), reg)]
    assert "R1  0/2 tool calls" in render_diagnosis(reps, has_tools=False)  # 1 call x 2 traces


def test_a_trace_with_nothing_missing_reports_no_gaps():
    reg = ToolRegistry.from_dict(
        {
            "tools": {
                "charge": {
                    "schema": {"type": "object", "properties": {"amt": {"type": "number"}}},
                    "metadata": {"side_effecting": True, "idempotent": True},
                }
            }
        }
    )
    trace = build_trace(
        "t",
        [
            ToolCall("c1", "charge", {"amt": 5}),
            ToolResult("c1", {"ok": True}, status=ResultStatus.OK),
        ],
    )
    assert "No instrumentation or contract gaps" in _diag(trace, reg, has_tools=True)
