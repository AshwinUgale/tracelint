"""Harbor ATIF adapter — normalize an ATIF trajectory (``agent/trajectory.json``) into the canonical
schema. Fixtures mirror the shapes real producers write (Harbor's Claude Code, Terminus, Kimi /
Pi, Strands converters, and a leaderboard submitter's ``extra.exit_code``)."""

from __future__ import annotations

import json

import pytest

from tracelint import (
    atif_tools_to_registry,
    default_rules,
    from_atif_trajectories,
    from_atif_trajectory,
    lint_atif_trajectory,
    lint_trace,
    load_source,
)
from tracelint.adapters.atif import is_atif
from tracelint.cli import main
from tracelint.findings import ConfidenceTier
from tracelint.trace import Message, ResultStatus, Role


def _call(call_id, name, args):
    return {"tool_call_id": call_id, "function_name": name, "arguments": args}


def _result(call_id, content, **extra):
    out = {"source_call_id": call_id, "content": content}
    if extra:
        out["extra"] = extra
    return out


def _agent(step_id, message="", calls=(), results=(), **fields):
    step = {"step_id": step_id, "source": "agent", "message": message, **fields}
    if calls:
        step["tool_calls"] = list(calls)
    if results:
        step["observation"] = {"results": list(results)}
    return step


def _trajectory(*steps, version="ATIF-v1.6", **root):
    return {
        "schema_version": version,
        "session_id": "sess-1",
        "agent": {"name": "test-agent", "version": "1.0", "model_name": "m-1"},
        "steps": [
            {"step_id": 1, "source": "system", "message": "You are a coding agent."},
            {"step_id": 2, "source": "user", "message": "Fix the failing test in /app."},
            *steps,
        ],
        **root,
    }


def _status_of(result):
    turn = _agent(3, calls=[_call("c1", "bash", {"cmd": "ls"})], results=[result])
    (res,) = from_atif_trajectory(_trajectory(turn)).tool_results()
    return res


# --- mapping ---------------------------------------------------------------------------


def test_steps_become_messages_and_calls_paired_with_results():
    trace = from_atif_trajectory(
        _trajectory(
            _agent(
                3,
                "Listing and reading.",
                calls=[_call("c1", "bash", {"cmd": "ls"}), _call("c2", "read", {"path": "a.py"})],
                results=[_result("c2", "print(1)"), _result("c1", "a.py")],
                reasoning_content="Two lookups at once.",
            ),
            _agent(4, "Done: the test passes now."),
        )
    )
    assert trace.run_id == "sess-1"
    roles = [s.role for s in trace.steps if isinstance(s, Message)]
    assert roles[:2] == [Role.SYSTEM, Role.USER]
    # reasoning, then the turn's message, then its calls, then their results
    assert [type(s).__name__ for s in trace.steps[2:]] == [
        "Message", "Message", "ToolCall", "ToolCall", "ToolResult", "ToolResult", "Message",
    ]
    ls, read = trace.tool_calls()
    assert (ls.name, ls.args) == ("bash", {"cmd": "ls"})
    assert trace.result_for(ls).content == "a.py"
    assert trace.result_for(read).content == "print(1)"
    assert trace.final == "Done: the test passes now."


def test_run_id_prefers_explicit_then_trajectory_id():
    doc = _trajectory(trajectory_id="traj-9")
    assert from_atif_trajectory(doc).run_id == "traj-9"
    assert from_atif_trajectory(doc, run_id="mine").run_id == "mine"
    del doc["session_id"], doc["trajectory_id"]
    assert from_atif_trajectory(doc).run_id == "atif-run"


def test_json_result_content_is_parsed_like_other_adapters():
    res = _status_of(_result("c1", '{"rows": 3}'))
    assert res.content == {"rows": 3}


def test_content_parts_are_flattened_to_their_text():
    trace = from_atif_trajectory(
        _trajectory(
            _agent(
                3,
                [{"type": "text", "text": "Looking at the screenshot."},
                 {"type": "image", "source": {"media_type": "image/png", "path": "images/1.png"}}],
                calls=[_call("c1", "click", {"x": 1})],
                results=[_result("c1", [{"type": "text", "text": "clicked"}])],
            )
        )
    )
    assert "Looking at the screenshot." in [s.content for s in trace.messages()]
    assert trace.tool_results()[0].content == "clicked"


def test_agent_turn_records_model_and_tokens():
    trace = from_atif_trajectory(
        _trajectory(_agent(3, "hi", metrics={"prompt_tokens": 120, "completion_tokens": 7}))
    )
    meta = trace.steps[2].meta
    assert (meta.model, meta.tokens_in, meta.tokens_out) == ("m-1", 120, 7)


def test_missing_tool_call_id_gets_a_stable_synthetic_id():
    doc = _trajectory(_agent(3, calls=[{"function_name": "bash", "arguments": {"cmd": "ls"}}]))
    assert from_atif_trajectory(doc).tool_calls()[0].call_id == from_atif_trajectory(
        doc
    ).tool_calls()[0].call_id != ""


# --- result status: structured signals only -----------------------------------------------


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (_result("c1", "boom", is_error=True), ResultStatus.ERROR),  # Kimi / Pi
        (_result("c1", "boom", tool_result_is_error=True), ResultStatus.ERROR),  # Claude Code
        # Harbor's Claude Code converter appends this line to a failed result
        (_result("c1", "<tool_use_error>bad</tool_use_error>\n\n[error] tool reported failure"),
         ResultStatus.ERROR),
        (_result("c1", "x", status="error"), ResultStatus.ERROR),  # Strands
        (_result("c1", "x", status="Failed"), ResultStatus.ERROR),
        (_result("c1", '{"error": "not found"}'), ResultStatus.ERROR),
        (_result("c1", "fine", is_error=False), ResultStatus.OK),
        (_result("c1", "fine", status="success"), ResultStatus.OK),
        (_result("c1", "fine", exit_code=0), ResultStatus.OK),  # a submitter's extra.exit_code
        (_result("c1", "fine", returncode="0"), ResultStatus.OK),
        # grep with no match / a repro script meant to fail: non-zero is not a failure
        (_result("c1", "", exit_code=1), ResultStatus.UNKNOWN),
        # Codex / Junie "completed" says the tool finished, not that it succeeded
        (_result("c1", "x", status="completed"), ResultStatus.UNKNOWN),
        (_result("c1", "plain output"), ResultStatus.UNKNOWN),
        # the marker only counts as a line of its own, not quoted inside output
        (_result("c1", "$ grep '[error] tool reported failure' log.txt"), ResultStatus.UNKNOWN),
    ],
)
def test_result_status_reads_producer_conventions(result, status):
    assert _status_of(result).status is status


@pytest.mark.parametrize(
    ("command", "exit_code", "output", "status"),
    [
        ("grep -n foo a.py", 1, "", ResultStatus.OK),  # searched, found nothing
        ("C-c", 130, "^C", ResultStatus.OK),  # the agent stopped a process on purpose
        ("grep -n foo missing.py", 2, "grep: missing.py: No such file", ResultStatus.UNKNOWN),
        ("pytest -q", 1, "1 failed", ResultStatus.UNKNOWN),
    ],
)
def test_exit_code_conventions(command, exit_code, output, status):
    turn = _agent(3, calls=[_call("c1", "bash", {"command": command})],
                  results=[_result("c1", output, exit_code=exit_code)])
    assert from_atif_trajectory(_trajectory(turn)).tool_results()[0].status is status


def test_structured_failure_is_an_r2a_hard_event_but_a_nonzero_exit_is_not():
    failed = _trajectory(
        _agent(3, calls=[_call("c1", "TodoWrite", {"todos": []})],
               results=[_result("c1", "InputValidationError", is_error=True)])
    )
    nonzero = _trajectory(
        _agent(3, calls=[_call("c1", "bash", {"cmd": "grep x f"})],
               results=[_result("c1", "", exit_code=1)])
    )
    hard = lint_atif_trajectory(failed).by_tier(ConfidenceTier.HARD_EVENT)
    assert any(f.rule == "R2a" for f in hard)
    assert not lint_atif_trajectory(nonzero).by_tier(ConfidenceTier.HARD_EVENT)


# --- observations without a source_call_id ------------------------------------------------


def test_terminus_batch_observation_pairs_with_the_last_call():
    # Terminus sends keystrokes and records one terminal screen per batch, with no source_call_id.
    screen = {"content": "New Terminal Output:\n$ ls\na.py\n$ cat a.py\nprint(1)"}
    trace = from_atif_trajectory(
        _trajectory(
            _agent(3, calls=[_call("k1", "bash_command", {"keystrokes": "ls\n"}),
                             _call("k2", "bash_command", {"keystrokes": "cat a.py\n"})],
                   results=[screen]),
            _agent(4, calls=[_call("k3", "bash_command", {"keystrokes": "C-c"})],
                   results=[{"content": "New Terminal Output:\n$"}]),
        )
    )
    first, last, single = trace.tool_calls()
    assert trace.result_for(first) is None  # the agent never saw its own output
    assert trace.result_for(last).content == screen["content"]
    assert trace.result_for(single).content == "New Terminal Output:\n$"
    assert not any(isinstance(s, Message) and s.content == screen["content"] for s in trace.steps)


def test_several_unlinked_results_are_not_guessed_onto_calls():
    trace = from_atif_trajectory(
        _trajectory(
            _agent(3, calls=[_call("a", "t", {}), _call("b", "t", {"n": 2})],
                   results=[{"content": "one"}, {"content": "two"}])
        )
    )
    assert not trace.tool_results()
    assert [s.content for s in trace.messages() if s.role is Role.SYSTEM][-2:] == ["one", "two"]


def test_system_step_observation_is_context_not_a_tool_result():
    summary = {
        "step_id": 3,
        "source": "system",
        "message": "Context compaction performed",
        "observation": {"results": [{"content": "Summary: user wants order A100 refunded."}]},
        "extra": {"context_management": {"type": "compaction", "boundary": "replace"}},
    }
    trace = from_atif_trajectory(_trajectory(summary))
    assert not trace.tool_results()
    assert trace.steps[-1].role is Role.SYSTEM
    assert "A100" in trace.steps[-1].content


def test_copied_context_is_not_replayed_as_new_calls():
    # After a summary, earlier turns are copied in (is_copied_context). Replaying their calls would
    # read as the agent repeating itself; their text stays as context the agent was shown.
    lookup = _call("c1", "get_order", {"id": "A100"})
    copied = _agent(3, "Looking it up.", calls=[lookup],
                    results=[_result("c1", '{"id": "A100", "total": 42}')], is_copied_context=True)
    real = _agent(4, calls=[_call("c9", "get_order", {"id": "A100"})],
                  results=[_result("c9", '{"id": "A100", "total": 42}')])
    trace = from_atif_trajectory(_trajectory(copied, real))
    assert [c.call_id for c in trace.tool_calls()] == ["c9"]
    assert any(isinstance(s, Message) and "total" in s.content for s in trace.steps)
    report = lint_trace(trace, default_rules(), None)
    assert not [f for f in report.active_findings if f.rule in ("R4", "R5")]


# --- arguments and tool definitions -------------------------------------------------------


def test_tool_definitions_attach_schemas_and_build_a_registry():
    schema = {
        "type": "object",
        "properties": {"ticker": {"type": "string"}, "metric": {"type": "string"}},
        "required": ["ticker", "metric"],
    }
    doc = _trajectory(_agent(3, calls=[_call("c1", "financial_search", {"ticker": "GOOGL"})],
                             results=[_result("c1", "185.35")]), version="ATIF-v1.5")
    doc["agent"]["tool_definitions"] = [
        {"type": "function", "function": {"name": "financial_search", "parameters": schema}}
    ]
    trace = from_atif_trajectory(doc)
    assert trace.tool_calls()[0].schema == schema  # discovery: `tracelint init`, R11

    registry = atif_tools_to_registry(doc)
    assert registry.schema_for("financial_search") == schema
    report = lint_trace(trace, default_rules(), registry)
    assert any(f.rule == "R1" for f in report.by_tier(ConfidenceTier.HARD_DEFECT))


def test_unparseable_argument_string_is_kept_as_evidence():
    doc = _trajectory(_agent(3, calls=[_call("c1", "bash", '{"cmd": "ls"')]))
    call = from_atif_trajectory(doc).tool_calls()[0]
    assert call.args == {} and call.raw_text == '{"cmd": "ls"'
    assert any(f.rule == "R6" for f in lint_atif_trajectory(doc).active_findings)


# --- subagents ------------------------------------------------------------------------------


def test_embedded_subagents_are_separate_runs():
    grandchild = {
        "schema_version": "ATIF-v1.7", "trajectory_id": "search-2",
        "agent": {"name": "searcher", "version": "1"},
        "steps": [{"step_id": 1, "source": "user", "message": "find it"}],
    }
    child = {
        "schema_version": "ATIF-v1.7", "trajectory_id": "search-1",
        "agent": {"name": "searcher", "version": "1"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "find the config"},
            _agent(2, calls=[_call("s1", "grep", {"q": "config"})],
                   results=[_result("s1", "app/config.py")]),
        ],
        "subagent_trajectories": [grandchild],
    }
    parent = _trajectory(
        _agent(3, calls=[_call("d1", "Task", {"prompt": "find the config"})],
               results=[{"source_call_id": "d1", "content": "app/config.py",
                         "subagent_trajectory_ref": [{"trajectory_id": "search-1"}]}]),
        version="ATIF-v1.7",
        subagent_trajectories=[child],
    )
    traces = from_atif_trajectories(parent)
    assert [t.run_id for t in traces] == ["sess-1", "sess-1/search-1", "sess-1/search-1/search-2"]
    assert [c.name for c in traces[0].tool_calls()] == ["Task"]  # not spliced into the parent
    assert [c.name for c in traces[1].tool_calls()] == ["grep"]


def test_malformed_subagents_are_skipped_not_fatal():
    parent = _trajectory(version="ATIF-v1.7")
    parent["subagent_trajectories"] = [None, {"trajectory_id": 1}, {"steps": "x"}, {"steps": []}]
    traces = from_atif_trajectories(parent)
    assert [t.run_id for t in traces] == ["sess-1", "sess-1/subagent-4"]


# --- detection and loading ------------------------------------------------------------------


def test_is_atif():
    assert is_atif(_trajectory())
    versionless = _trajectory()
    del versionless["schema_version"]
    assert is_atif(versionless)
    assert not is_atif({"run_id": "n", "steps": [{"type": "message", "role": "user"}]})
    assert not is_atif({"agent": "x", "success": True})  # a custom non-ATIF trajectory.json
    assert not is_atif([])


def _write(tmp_path, obj, name="trajectory.json"):
    path = tmp_path / name
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def test_load_source_reads_one_many_and_jsonl(tmp_path):
    doc = _trajectory(_agent(3, calls=[_call("c1", "bash", {"cmd": "ls"})]))
    assert len(load_source(_write(tmp_path, doc), "atif")) == 1
    assert len(load_source(_write(tmp_path, [doc, doc], "many.json"), "atif")) == 2
    jsonl = tmp_path / "runs.jsonl"
    jsonl.write_text(json.dumps(doc) + "\n" + json.dumps(doc) + "\n", encoding="utf-8")
    assert len(load_source(str(jsonl), "atif")) == 2


def test_atif_read_as_native_names_the_format(tmp_path):
    # An ATIF trajectory also has a `steps` list; read as native it must not be half-parsed.
    with pytest.raises(ValueError, match="Harbor ATIF trajectories: use --format atif"):
        load_source(_write(tmp_path, _trajectory()), "native")


@pytest.mark.parametrize(
    ("doc", "hint"),
    [
        ({"run_id": "n", "steps": [{"type": "message", "role": "user", "content": "hi"}]},
         "omit --format"),
        ([{"role": "user", "content": "hi"}], "use --format openai"),
    ],
)
def test_other_formats_read_as_atif_name_theirs(tmp_path, doc, hint):
    with pytest.raises(ValueError, match=hint):
        load_source(_write(tmp_path, doc), "atif")


def test_cli_checks_an_atif_trajectory(tmp_path, capsys):
    doc = _trajectory(_agent(3, calls=[_call("c1", "bash", {"cmd": "ls"})],
                             results=[_result("c1", "a.py", exit_code=0)]))
    path = _write(tmp_path, doc)
    assert main(["check", path, "--format", "atif", "--quiet"]) == 0
    assert main(["check", path, "--quiet"]) == 3  # read as native: an input error, not a pass
