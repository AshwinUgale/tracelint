"""Record quality — is the trace itself complete enough to trust? (``tracelint.record``)

The checks judge the record, never the agent: results and outcomes recorded, arguments recorded,
reasoning recorded instead of a placeholder, a record that stops on an unanswered call, subagent
runs that were launched but not recorded, duplicate call ids, redacted values. ``tracelint doctor``
reports them, advisory unless ``--fail-under`` is set.
"""

from __future__ import annotations

import json

import pytest

from tracelint import build_trace
from tracelint.cli import main
from tracelint.record import (
    below_thresholds,
    check_record,
    check_records,
    parse_thresholds,
    render_record_quality,
    summarize,
)
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult


def _turn(text):
    return Message(Role.ASSISTANT, text)


def test_a_complete_record_has_no_issues():
    trace = build_trace(
        "t",
        [
            Message(Role.USER, "charge the card"),
            _turn("I'll charge it now."),
            ToolCall("c1", "charge", {"amt": 5}),
            ToolResult("c1", {"ok": True}, status=ResultStatus.OK),
            _turn("Done."),
        ],
    )
    c = check_record(trace)
    assert (c.calls, c.results_recorded, c.outcome_recorded, c.arguments_recorded) == (1, 1, 1, 1)
    assert c.reasoning_recorded == 2 and c.issues() == []


def test_missing_results_outcomes_and_arguments_are_counted():
    no_args = ToolCall("c3", "search", {})
    no_args.args_unavailable = "not exported"
    trace = build_trace(
        "t",
        [
            ToolCall("c1", "read", {"p": "a"}),
            ToolResult("c1", "text"),  # recorded, but no success/failure signal
            ToolCall("c2", "read", {"p": "b"}),  # never answered...
            no_args,
            ToolResult("c3", "hits", status=ResultStatus.ERROR),
            _turn("next"),  # ...but the record goes on, so it doesn't end unanswered
        ],
    )
    c = check_record(trace)
    assert (c.calls, c.results_recorded, c.outcome_recorded, c.arguments_recorded) == (3, 2, 1, 2)
    assert not c.ends_unanswered
    assert "1/3 calls have no result" in c.issues() and "1/3 calls have no arguments" in c.issues()


def test_a_record_that_stops_on_an_unanswered_call():
    trace = build_trace("t", [_turn("running it"), ToolCall("c1", "bash", {"cmd": "make"})])
    assert check_record(trace).ends_unanswered
    assert "ends on a call with no result" in check_record(trace).issues()


def test_placeholder_and_empty_turns_are_not_reasoning():
    trace = build_trace(
        "t",
        [
            _turn("Executed exec_command call_8Fh2"),
            _turn("Tool call"),
            _turn("   "),
            _turn("The test fails because the parser drops the last line."),
        ],
    )
    c = check_record(trace)
    assert (c.assistant_turns, c.placeholder_turns, c.empty_turns, c.reasoning_recorded) == (
        4,
        2,
        1,
        1,
    )
    # "Executed" inside real prose is not a placeholder
    assert (
        check_record(
            build_trace("t", [_turn("Executed the migration; next I verify it.\nOK")])
        ).placeholder_turns
        == 0
    )


def test_launched_subagents_need_their_runs_in_the_batch():
    parent = build_trace(
        "job",
        [
            ToolCall("c1", "spawn_agent", {"agent_type": "explorer"}),
            ToolResult("c1", {"agent_name": "Blake"}),
            ToolCall("c2", "read_subagent", {"id": "Blake"}),  # reads output: not a launch
            ToolResult("c2", "still running"),
        ],
    )
    (alone,) = check_records([parent])
    assert alone.subagent_launches == 1 and alone.subagents_missing == 1
    assert "1 subagent run(s) not recorded" in alone.issues()
    child = build_trace("job/Blake", [_turn("exploring")])
    with_child = {c.run_id: c for c in check_records([parent, child])}
    assert with_child["job"].subagents_missing == 0
    # Claude Code's subagent tool names count as launches too
    assert (
        check_record(build_trace("t", [ToolCall("c1", "Task", {"prompt": "x"})])).subagent_launches
        == 1
    )


def test_duplicate_call_ids_and_redactions():
    trace = build_trace(
        "t",
        [
            ToolCall("c1", "read", {"p": "a"}),
            ToolResult("c1", "x"),
            ToolCall("c1", "read", {"p": "b"}),
            ToolResult("c1", {"token": "[REDACTED]", "path": "<REDACTED_PATH>"}),
        ],
    )
    c = check_record(trace)
    assert c.duplicate_call_ids == 1 and c.redactions == 2


def test_summary_coverage_and_thresholds():
    good = build_trace("a", [ToolCall("c1", "x", {}), ToolResult("c1", 1, status=ResultStatus.OK)])
    bad = build_trace("b", [ToolCall("c1", "x", {}), ToolCall("c2", "x", {}), ToolResult("c2", 1)])
    s = summarize(check_records([good, bad]))
    assert s.coverage() == {"results": 66.7, "outcome": 50.0, "arguments": 100.0, "reasoning": None}
    assert below_thresholds(s, parse_thresholds(["results=90", "arguments=90"])) == [
        "results 66.7% < 90%"
    ]
    assert "results recorded" in render_record_quality(s)
    with pytest.raises(ValueError, match="NAME=PERCENT"):
        parse_thresholds(["speed=9"])


def _write(path, trace):
    path.write_text(json.dumps(trace), encoding="utf-8")
    return str(path)


def _native(run_id, steps):
    return {"run_id": run_id, "steps": steps}


def test_doctor_reports_record_quality_and_survives_unreadable_files(tmp_path, capsys):
    a = tmp_path / "harness-a"
    b = tmp_path / "harness-b"
    a.mkdir()
    b.mkdir()
    ok = _write(
        a / "t1.json",
        _native(
            "t1",
            [
                {"type": "tool_call", "call_id": "c1", "name": "read", "args": {"p": "x"}},
                {"type": "tool_result", "call_id": "c1", "content": "ok", "status": "ok"},
            ],
        ),
    )
    open_end = _write(
        b / "t2.json",
        _native(
            "t2",
            [
                {"type": "tool_call", "call_id": "c1", "name": "read", "args": {"p": "x"}},
            ],
        ),
    )
    empty = b / "empty.json"
    empty.write_text("", encoding="utf-8")
    out_json = tmp_path / "record.json"
    code = main(["doctor", ok, open_end, str(empty), "--group-by", "dir", "--json", str(out_json)])
    out = capsys.readouterr().out
    assert code == 0  # advisory
    assert "Record quality (2 trace(s), 2 tool call(s))" in out
    assert "ends on an unanswered call" in out and "unreadable files" in out
    assert "harness-a" in out and "harness-b" in out
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["summary"]["coverage"]["results"] == 50.0
    assert {t["run_id"] for t in data["traces"]} == {"t1", "t2"}
    assert any("empty.json" in u for u in data["summary"]["unreadable"])


def test_doctor_fail_under_gates_with_exit_1(tmp_path, capsys):
    p = _write(
        tmp_path / "t.json",
        _native(
            "t",
            [
                {"type": "tool_call", "call_id": "c1", "name": "read", "args": {"p": "x"}},
                {"type": "tool_call", "call_id": "c2", "name": "read", "args": {"p": "y"}},
                {"type": "tool_result", "call_id": "c2", "content": "ok"},
            ],
        ),
    )
    assert main(["doctor", p, "--fail-under", "results=90"]) == 1
    assert "Below --fail-under: results 50.0% < 90%" in capsys.readouterr().out
    assert main(["doctor", p, "--fail-under", "results=40"]) == 0


def test_doctor_with_nothing_readable_is_an_input_error(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert main(["doctor", str(empty)]) == 3
