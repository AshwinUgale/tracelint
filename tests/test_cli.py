"""Phase 1c — the `tracelint check` CLI and its CI exit-code contract (spec §II.10)."""

from __future__ import annotations

import json

import pytest

from tracelint.agent import ReActAgent, ScriptedLLM, build_demo_toolset, final, run_demo, tool
from tracelint.cli import main


def _write_trace(tmp_path, trace, name="trace.json"):
    p = tmp_path / name
    p.write_text(trace.to_json(), encoding="utf-8")
    return str(p)


def _write_tools(tmp_path, toolset, name="tools.json"):
    p = tmp_path / name
    specs = {}
    for tname in toolset.names():
        spec = toolset.to_registry().get(tname)
        specs[tname] = {"schema": spec.schema}
    p.write_text(json.dumps({"tools": specs}), encoding="utf-8")
    return str(p)


def _planted_trace():
    toolset = build_demo_toolset()
    script = [tool("cancel_order", {"order_id": 4521, "reason": "fraud"}), final("done")]
    return ReActAgent(ScriptedLLM(script), toolset).run("cancel", run_id="planted"), toolset


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "tracelint" in capsys.readouterr().out


def test_no_command_prints_help():
    assert main([]) == 0


def test_check_clean_trace_exits_zero(tmp_path, capsys):
    trace, toolset = run_demo()
    tp = _write_trace(tmp_path, trace)
    tt = _write_tools(tmp_path, toolset)
    code = main(["check", tp, "--tools", tt])
    assert code == 0
    assert "clean" in capsys.readouterr().out


def test_check_planted_violation_exits_two(tmp_path, capsys):
    trace, toolset = _planted_trace()
    tp = _write_trace(tmp_path, trace)
    tt = _write_tools(tmp_path, toolset)
    code = main(["check", tp, "--tools", tt])
    assert code == 2
    out = capsys.readouterr().out
    assert "hard_defect" in out and "R1" in out


def test_check_json_output_written(tmp_path):
    trace, toolset = _planted_trace()
    tp = _write_trace(tmp_path, trace)
    tt = _write_tools(tmp_path, toolset)
    out = tmp_path / "out.json"
    code = main(["check", tp, "--tools", tt, "--json", str(out), "--quiet"])
    assert code == 2
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["overall_exit"] == 2
    assert data["reports"][0]["findings"][0]["rule"] == "R1"


def test_check_without_tools_suppresses_and_passes(tmp_path, capsys):
    # No --tools: R1 has no schema, so it suppresses (disclosed) and CI does not fail.
    trace, _toolset = _planted_trace()
    tp = _write_trace(tmp_path, trace)
    code = main(["check", tp])
    assert code == 0
    assert "suppressed" in capsys.readouterr().out


def test_check_unknown_rule_is_input_error(tmp_path):
    trace, _ = run_demo()
    tp = _write_trace(tmp_path, trace)
    assert main(["check", tp, "--rules", "R99"]) == 3


def test_check_missing_file_is_input_error(capsys):
    assert main(["check", "does_not_exist.json"]) == 3
    assert "error" in capsys.readouterr().err


def test_check_rules_subset_selects_r1(tmp_path):
    trace, toolset = _planted_trace()
    tp = _write_trace(tmp_path, trace)
    tt = _write_tools(tmp_path, toolset)
    assert main(["check", tp, "--tools", tt, "--rules", "R1", "--quiet"]) == 2


def test_scorecard_demo_robust(capsys):
    code = main(["scorecard", "--demo", "--faults", "error", "--runs", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "recovery scorecard" in out and "correctness recovery" in out
    assert "error" in out


def test_scorecard_demo_buggy_shows_low_recovery(capsys):
    main(["scorecard", "--demo", "--buggy", "--faults", "error"])
    out = capsys.readouterr().out
    assert "0/1" in out or "rate=0.00" in out


def test_scorecard_unknown_fault_is_input_error():
    assert main(["scorecard", "--demo", "--faults", "meltdown"]) == 3


def test_demo_runs_validation_and_scorecard(capsys):
    code = main(["demo", "--runs", "1"])
    assert code == 0  # all validation cases behave as expected → clean self-check
    out = capsys.readouterr().out
    assert "cases behaved as expected" in out
    assert "FAIL" not in out  # every planted defect recovered, every control silent
    assert "recovery scorecard" in out


def test_demo_writes_html(tmp_path):
    out = tmp_path / "demo.html"
    code = main(["demo", "--runs", "1", "--html", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_check_writes_html(tmp_path):
    trace, toolset = _planted_trace()
    tp = _write_trace(tmp_path, trace)
    tt = _write_tools(tmp_path, toolset)
    out = tmp_path / "report.html"
    main(["check", tp, "--tools", tt, "--html", str(out), "--quiet"])
    assert "hard_defect" in out.read_text(encoding="utf-8")


def _write_openinference_spans(tmp_path, name="spans.json"):
    """A Phoenix-shaped OpenInference export: book_flight is missing a required arg (R1)."""
    spans = [
        {
            "span_id": "s1",
            "trace_id": "oi-run",
            "start_time": "2024-06-01T10:00:01Z",
            "name": "book_flight",
            "status_code": "OK",
            "attributes": {
                "openinference.span.kind": "TOOL",
                "tool.name": "book_flight",
                "input.value": json.dumps({"flight_id": "AA100"}),
                "output.value": json.dumps({"status": "error"}),
            },
        }
    ]
    p = tmp_path / name
    p.write_text(json.dumps(spans), encoding="utf-8")
    return str(p)


def _write_flight_tools(tmp_path, name="flight_tools.json"):
    schema = {
        "type": "object",
        "properties": {"flight_id": {"type": "string"}, "passenger": {"type": "string"}},
        "required": ["flight_id", "passenger"],
    }
    p = tmp_path / name
    p.write_text(json.dumps({"tools": {"book_flight": {"schema": schema}}}), encoding="utf-8")
    return str(p)


def test_check_openinference_format_with_tools_exits_two(tmp_path, capsys):
    sp = _write_openinference_spans(tmp_path)
    tt = _write_flight_tools(tmp_path)
    code = main(["check", sp, "--format", "openinference", "--tools", tt])
    assert code == 2
    out = capsys.readouterr().out
    assert "hard_defect" in out and "R1" in out


def test_check_openinference_format_keyless_suppresses_and_passes(tmp_path, capsys):
    # No --tools: R1 suppresses (disclosed), no hard defect from the spans alone → exit 0.
    sp = _write_openinference_spans(tmp_path)
    code = main(["check", sp, "--format", "openinference"])
    assert code == 0
    assert "suppressed" in capsys.readouterr().out


def test_check_unknown_format_is_input_error(tmp_path):
    sp = _write_openinference_spans(tmp_path)
    # A usage error exits 3 (input error), not argparse's 2 — the code reserved for a hard defect.
    with pytest.raises(SystemExit) as exc:
        main(["check", sp, "--format", "bogus"])
    assert exc.value.code == 3


@pytest.mark.parametrize(
    "argv",
    [
        ["check", "trace.json", "--fromat", "openinference"],  # a mistyped flag
        ["check"],  # a missing required argument
        ["chek", "trace.json"],  # an unknown command
        ["langfuse", "pull"],  # a nested subcommand's usage error
    ],
)
def test_usage_errors_exit_three_not_two(argv, capsys):
    # A misconfigured CI step must not read as "defect found" (exit 2).
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 3
    assert "error:" in capsys.readouterr().err


def test_a_report_the_console_cannot_encode_does_not_crash(tmp_path, monkeypatch):
    # Redirected output on Windows uses the locale code page; a trace's own text (CJK here) used to
    # raise UnicodeEncodeError, which turned a clean run into exit 3.
    import io
    import sys

    trace = {
        "run_id": "予約-東京",
        "steps": [{"type": "message", "role": "user", "content": "予約"}],
    }
    p = tmp_path / "\u4e88\u7d04.json"
    p.write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert main(["check", str(p)]) == 0
    sys.stdout.flush()
    assert rb"\u4e88" in raw.getvalue()  # 予, escaped rather than a crash


def test_check_span_file_with_the_default_format_is_an_input_error(tmp_path, capsys):
    # No --format means native — also the GitHub Action's default. A span file read as native used
    # to lint as one empty trace per span and exit 0; now it fails the step and names the fix.
    sp = _write_openinference_spans(tmp_path)
    assert main(["check", sp]) == 3
    err = capsys.readouterr().err
    assert "it looks like OpenInference / OTel spans: use --format openinference" in err


def test_check_empty_trace_file_is_an_input_error(tmp_path, capsys):
    p = tmp_path / "traces.jsonl"
    p.write_text("", encoding="utf-8")
    assert main(["check", str(p), "--format", "openinference"]) == 3
    assert "no traces found" in capsys.readouterr().err


def test_init_on_the_wrong_format_is_an_input_error(tmp_path, capsys):
    sp = _write_openinference_spans(tmp_path)
    assert main(["init", sp]) == 3
    assert "use --format openinference" in capsys.readouterr().err


def test_check_json_separates_suppressions_and_carries_the_source(tmp_path):
    # Without --tools, R1 cannot run: it is disclosed in `suppressions`, never folded into
    # `findings` (whose count must match the text report). Each report also carries its file path.
    trace, _toolset = _planted_trace()
    tp = _write_trace(tmp_path, trace)
    out = tmp_path / "out.json"
    assert main(["check", tp, "--json", str(out), "--quiet"]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))["reports"][0]
    assert any(s["rule"] == "R1" for s in report["suppressions"])
    assert all(f["rule"] != "R1" for f in report["findings"])
    assert report["source"].endswith(".json")
