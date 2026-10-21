"""The Inspect chat-message reader (Inspect AI, and every transcript Inspect Scout imports).

Messages are given in their JSON form (``ChatMessage.model_dump()``), which the adapter reads
without Inspect installed; ``test_scout.py`` runs the same reader on Inspect's own objects.
"""

from __future__ import annotations

import pytest

from tracelint.adapters import from_inspect_messages, inspect_trace, subagent_calls
from tracelint.findings import ConfidenceTier
from tracelint.rules import lint_trace, select_rules
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult


def _assistant(mid, calls=(), content="", **extra):
    return {"id": mid, "role": "assistant", "content": content, "tool_calls": list(calls), **extra}


def _call(cid, function, arguments=None, parse_error=None):
    return {
        "id": cid,
        "function": function,
        "arguments": arguments or {},
        "parse_error": parse_error,
    }


def _tool(mid, call_id, content="", error=None, function=None):
    return {
        "id": mid,
        "role": "tool",
        "content": content,
        "tool_call_id": call_id,
        "function": function,
        "error": error,
    }


def test_messages_calls_and_results_map_with_positions_and_ids():
    messages = [
        {"id": "s", "role": "system", "content": "be helpful"},
        {"id": "u", "role": "user", "content": [{"type": "text", "text": "find order 7"}]},
        _assistant(
            "a",
            [_call("c1", "get_order", {"order_id": 7})],
            content=[
                {"type": "reasoning", "reasoning": "look it up", "redacted": False},
                {"type": "text", "text": "Looking."},
            ],
            model="openai/gpt-5",
        ),
        _tool("t", "c1", '{"id": 7, "status": "shipped"}'),
        _assistant("f", content="It shipped."),
    ]
    trace, positions = inspect_trace(messages, run_id="r1")
    kinds = [(type(s).__name__, getattr(s, "role", None)) for s in trace.steps]
    assert kinds == [
        ("Message", Role.SYSTEM),
        ("Message", Role.USER),
        ("Message", Role.ASSISTANT),  # reasoning
        ("Message", Role.ASSISTANT),  # text
        ("ToolCall", None),
        ("ToolResult", None),
        ("Message", Role.ASSISTANT),
    ]
    assert positions == [0, 1, 2, 2, 2, 3, 4]
    assert [s.source.observation_id for s in trace.steps] == ["s", "u", "a", "a", "a", "t", "f"]
    assert trace.steps[2].meta.model == "openai/gpt-5"
    call, result = trace.tool_calls()[0], trace.tool_results()[0]
    assert (call.name, call.args) == ("get_order", {"order_id": 7})
    assert trace.result_for(call) is result
    assert result.content == {"id": 7, "status": "shipped"}  # serialized JSON is parsed
    assert result.status is ResultStatus.UNKNOWN  # Inspect records failure, never success
    assert trace.final == "It shipped."
    assert trace.run_id == "r1"


def test_tool_call_error_is_a_structured_failure():
    trace = from_inspect_messages(
        [
            _assistant("a", [_call("c1", "bash", {"cmd": "sleep 99"})]),
            _tool("t", "c1", "", error={"type": "timeout", "message": "timed out after 30s"}),
        ]
    )
    result = trace.tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert result.error == "timeout: timed out after 30s"
    (finding,) = lint_trace(trace, select_rules(["R2a"])).active_findings
    assert finding.tier is ConfidenceTier.HARD_EVENT


def test_an_error_field_in_a_json_result_is_a_failure():
    trace = from_inspect_messages(
        [_assistant("a", [_call("c1", "api")]), _tool("t", "c1", '{"error": "quota exceeded"}')]
    )
    assert trace.tool_results()[0].status is ResultStatus.ERROR
    assert trace.tool_results()[0].error == "quota exceeded"


def test_a_failure_recorded_only_on_the_tool_event_is_read():
    # Scout's importers (Claude Code's is_error, a failed span) set the tool *event*'s error and
    # leave the message's empty.
    messages = [
        _assistant("a", [_call("c1", "Read", {"path": "x"}), _call("c2", "Read", {"path": "y"})]),
        _tool("t1", "c1", "File does not exist."),
        _tool("t2", "c2", "contents"),
    ]
    events = [
        {"event": "model"},
        {"event": "tool", "id": "c1", "error": {"type": "unknown",
                                       "message": "File does not exist."}},
        {"event": "tool", "id": "c2", "error": None},
    ]
    results = from_inspect_messages(messages, events=events).tool_results()
    assert [r.status for r in results] == [ResultStatus.ERROR, ResultStatus.UNKNOWN]
    assert results[0].error == "unknown: File does not exist."
    # without the events the failure is invisible
    assert from_inspect_messages(messages).tool_results()[0].status is ResultStatus.UNKNOWN


def test_harbors_failure_line_is_read_as_in_the_atif_reader():
    # Scout's ATIF importer keeps the line Harbor's Claude Code converter appends to a failed
    # result, but not the is_error flag behind it.
    text = "Exit code 1\nboom\n[error] tool reported failure"
    trace = from_inspect_messages([_assistant("a", [_call("c1", "Bash")]), _tool("t", "c1", text)])
    assert trace.tool_results()[0].status is ResultStatus.ERROR
    mention = "the docs say '[error] tool reported failure' marks it"
    trace = from_inspect_messages(
        [_assistant("a", [_call("c1", "Bash")]), _tool("t", "c1", mention)]
    )
    assert trace.tool_results()[0].status is ResultStatus.UNKNOWN  # only the line on its own


def test_a_tool_event_marked_failed_without_an_error_is_a_failure():
    messages = [_assistant("a", [_call("c1", "x")]), _tool("t", "c1", "")]
    events = [{"event": "tool", "id": "c1", "error": None, "failed": True}]
    assert from_inspect_messages(messages, events=events).tool_results()[0].status is (
        ResultStatus.ERROR
    )


def test_arguments_inspect_could_not_parse_are_kept_for_r6():
    parse_error = (
        "Error parsing the following tool call arguments:\n\n"
        '{"order_id": 7\n\nError details: Expecting \',\' delimiter'
    )
    trace = from_inspect_messages(
        [_assistant("a", [_call("c1", "get_order", {}, parse_error=parse_error)])]
    )
    call = trace.tool_calls()[0]
    assert call.args == {}
    assert call.raw_text == '{"order_id": 7'
    assert call.args_unavailable is None  # the model's own text: proof, not a lossy record
    (finding,) = lint_trace(trace, select_rules(["R6"])).active_findings
    assert finding.tier is ConfidenceTier.HARD_DEFECT


def test_a_parse_error_in_another_format_is_kept_whole():
    trace = from_inspect_messages(
        [_assistant("a", [_call("c1", "f", {}, parse_error="arguments were not JSON")])]
    )
    assert trace.tool_calls()[0].raw_text == "arguments were not JSON"


def test_server_side_tool_use_becomes_a_paired_call_and_result():
    block = {
        "type": "tool_use",
        "tool_type": "web_search",
        "id": "ws1",
        "name": "web_search",
        "arguments": '{"query": "weather"}',
        "result": "sunny",
        "error": None,
    }
    text = {"type": "text", "text": "ok"}
    trace = from_inspect_messages([_assistant("a", content=[block, text])])
    call, result = trace.tool_calls()[0], trace.tool_results()[0]
    assert (call.name, call.args, call.call_id) == ("web_search", {"query": "weather"}, "ws1")
    assert trace.result_for(call) is result
    assert result.content == "sunny"
    assert isinstance(trace.steps[-1], Message) and trace.steps[-1].content == "ok"


def test_server_side_tool_error_and_broken_arguments():
    block = {
        "type": "tool_use",
        "tool_type": "mcp_call",
        "id": "m1",
        "name": "lookup",
        "arguments": '{"q": ',
        "result": "",
        "error": "server unavailable",
    }
    trace = from_inspect_messages([_assistant("a", content=[block])])
    call, result = trace.tool_calls()[0], trace.tool_results()[0]
    assert result.status is ResultStatus.ERROR and result.error == "server unavailable"
    # the provider's record of the call, not the model's text: never proof of a malformed call
    assert call.args_unavailable is not None
    (finding,) = lint_trace(trace, select_rules(["R6"])).active_findings
    assert finding.tier is ConfidenceTier.CANDIDATE


def test_a_tool_message_without_a_call_id_pairs_with_the_turns_next_call():
    # older Inspect logs recorded tool messages without tool_call_id
    trace = from_inspect_messages(
        [
            _assistant("a", [_call("c1", "ls"), _call("c2", "cat")]),
            _tool("t1", None, "a.txt", function="ls"),
            _tool("t2", None, "hello", function="cat"),
        ]
    )
    first, second = trace.tool_calls()
    assert trace.result_for(first).content == "a.txt"
    assert trace.result_for(second).content == "hello"


def test_redacted_reasoning_and_empty_text_add_no_steps():
    trace = from_inspect_messages(
        [
            _assistant(
                "a",
                content=[{"type": "reasoning", "reasoning": "xyz==", "redacted": True}],
            ),
            {"id": "u", "role": "user", "content": [{"type": "image", "image": "data:..."}]},
        ]
    )
    assert trace.steps == []


@pytest.mark.parametrize("bad", ["a string", {"messages": []}, 42])
def test_rejects_input_that_is_not_a_message_list(bad):
    with pytest.raises(TypeError):
        inspect_trace(bad)


def test_steps_are_canonical_types():
    trace = from_inspect_messages(
        [_assistant("a", [_call("c1", "x")]), _tool("t", "c1", "y")]
    )
    assert isinstance(trace.steps[0], ToolCall) and isinstance(trace.steps[1], ToolResult)


def test_subagent_calls_are_launches_whose_run_the_events_record():
    events = [
        {"event": "tool", "id": "c1", "agent_span_id": "agent-c1"},
        {"event": "span_begin", "id": "agent-c1", "type": "agent", "name": "general-purpose"},
        {"event": "tool", "id": "c2", "agent_span_id": "agent-c2"},  # its span is not there
        {"event": "tool", "id": "c3", "agent": "researcher"},  # a handoff, in the thread itself
        {"event": "tool", "id": "c4"},
    ]
    assert subagent_calls(events) == {"c1", "c3"}
    assert subagent_calls(None) == set()


def test_calls_recorded_without_an_id_get_distinct_ids():
    # e.g. a producer's server-side web_search_call with tool_call_id "" in two turns
    trace = from_inspect_messages(
        [
            _assistant("a1", [_call("", "web_search", {"query": "x"})]),
            _tool("t1", "", "r1", function="web_search"),
            _assistant("a2", [_call("", "web_search", {"query": "y"})]),
            _tool("t2", "", "r2", function="web_search"),
        ]
    )
    first, second = trace.tool_calls()
    assert first.call_id != second.call_id
    assert trace.result_for(first).content == "r1"
    assert trace.result_for(second).content == "r2"
