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
