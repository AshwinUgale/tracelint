"""One rule for reading a tool's result, in every adapter.

A provider records what a tool returned in its own way. OpenAI tool messages carry the result as a
JSON string. LangChain wraps it in a ``ToolMessage``: nested under ``data`` as OpenInference stores
it, flat in Langfuse and LangSmith, and LangSmith adds ``{"output": ...}`` around that. Up to 0.9
each adapter unwrapped (or didn't) and decided what counts as an error on its own:

- The same declined charge failed CI from OTel and an OpenAI dict, but passed from an OpenAI JSON
  string, Langfuse and LangSmith: the ``failure_when`` pointer never reached the result.
- ``"error": false`` or ``""`` counted as an error, and so did a status code in the result's body
  (a link checker's ``status_code: 404``), failing CI on correct runs.
- A string ``http_status`` (``"404"``) crashed the run with exit 3.

``fixtures/results`` holds real instrumentation output, generated offline with a scripted model: a
charge is declined and the agent ships the order anyway. One trace is LangGraph via OpenInference;
the other is LangChain's LangSmith tracer, with a stub client recording the runs it would send.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracelint import (
    Message,
    ResultStatus,
    Role,
    ToolCall,
    ToolRegistry,
    ToolResult,
    Trace,
    default_rules,
    from_langfuse_trace,
    from_langsmith_run,
    from_openai_messages,
    from_otel_spans,
    lint_trace,
)
from tracelint.findings import ConfidenceTier, LintReport

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "results"

# The operator's contract: a declined charge is a failure, and both tools change the world.
CHARGE_CONTRACT = ToolRegistry.from_dict(
    {
        "tools": {
            "charge_card": {
                "metadata": {
                    "side_effecting": True,
                    "failure_when": {"pointer": "/status", "in": ["declined"]},
                }
            },
            "ship_order": {"metadata": {"side_effecting": True}},
        }
    }
)
DECLINED = {"status": "declined", "charge_id": "ch_DECLINED_77", "order_id": "A100"}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _rules(report: LintReport, tier: ConfidenceTier) -> list[str]:
    return sorted(f.rule for f in report.active_findings if f.tier is tier)


# --- The same declined charge, from every source ----------------------------------------------


def _openai(content: Any) -> Trace:
    return from_openai_messages(
        [
            {"role": "user", "content": "Charge order A100 (4999) and ship it."},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "charge_card",
                            "arguments": json.dumps({"order_id": "A100", "amount": 4999}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": content},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c2",
                        "function": {
                            "name": "ship_order",
                            "arguments": json.dumps(
                                {"order_id": "A100", "charge_id": "ch_DECLINED_77"}
                            ),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"shipped": True})},
        ]
    )


def _langfuse_tool_message() -> Trace:
    # Langfuse's LangChain integration records the flat serialized ToolMessage.
    def message(content: Any, call_id: str) -> dict[str, Any]:
        return {
            "content": json.dumps(content),
            "additional_kwargs": {},
            "response_metadata": {},
            "type": "tool",
            "tool_call_id": call_id,
            "status": "success",
        }

    observations = [
        {
            "id": "o1",
            "type": "tool",
            "name": "charge_card",
            "startTime": "2026-10-01T10:00:01.000Z",
            "input": {"order_id": "A100", "amount": 4999},
            "output": message(DECLINED, "c1"),
        },
        {
            "id": "o2",
            "type": "tool",
            "name": "ship_order",
            "startTime": "2026-10-01T10:00:02.000Z",
            "input": {"order_id": "A100", "charge_id": "ch_DECLINED_77"},
            "output": message({"shipped": True}, "c2"),
        },
    ]
    return from_langfuse_trace({"id": "lf", "observations": observations})


@pytest.mark.parametrize(
    "build",
    [
        lambda: _openai(DECLINED),
        lambda: _openai(json.dumps(DECLINED)),
        _langfuse_tool_message,
        lambda: from_langsmith_run(_load("langsmith_declined_run.json")),
        lambda: from_otel_spans(_load("langgraph_declined_spans.json")),
    ],
    ids=["openai-dict", "openai-json-string", "langfuse-toolmessage", "langsmith", "otel"],
)
def test_a_declined_charge_then_shipping_is_a_defect_from_every_source(build):
    trace = build()
    charge = trace.tool_calls()[0]
    assert trace.result_for(charge).content["status"] == "declined"  # the tool's own result

    report = lint_trace(trace, default_rules(), CHARGE_CONTRACT)
    assert report.exit_code == 2
    assert _rules(report, ConfidenceTier.HARD_DEFECT) == ["R2b"]
    assert "R2a" in _rules(report, ConfidenceTier.HARD_EVENT)


# --- What counts as an error: the same rule in every adapter ---------------------------------


def _otel_result(output: Any) -> ToolResult:
    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "get_order",
        "input.value": json.dumps({"order_id": "A100"}),
        "output.value": json.dumps(output),
    }
    return from_otel_spans(
        [{"name": "get_order", "span_id": "s1", "attributes": attrs}]
    ).tool_results()[0]


def _langfuse_result(output: Any) -> ToolResult:
    obs = {"id": "o1", "type": "tool", "name": "get_order", "input": {}, "output": output}
    return from_langfuse_trace({"id": "lf", "observations": [obs]}).tool_results()[0]


def _langsmith_result(output: Any) -> ToolResult:
    run = {"id": "r1", "run_type": "tool", "name": "get_order", "inputs": {}, "outputs": output}
    return from_langsmith_run(run).tool_results()[0]


def _openai_result(output: Any) -> ToolResult:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "get_order"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(output)},
    ]
    return from_openai_messages(messages).tool_results()[0]


ADAPTERS = [_otel_result, _langfuse_result, _langsmith_result, _openai_result]
ADAPTER_IDS = ["otel", "langfuse", "langsmith", "openai"]


@pytest.mark.parametrize("read", ADAPTERS, ids=ADAPTER_IDS)
@pytest.mark.parametrize("no_error", [False, "", None, 0])
def test_a_falsy_error_field_is_not_an_error(read, no_error):
    # {"error": false} is how many APIs say "it worked"; 0.9 read any non-None value as an error.
    result = read({"error": no_error, "data": {"order_id": "A100"}})
    assert result.status is not ResultStatus.ERROR
    assert result.error is None


@pytest.mark.parametrize("read", ADAPTERS, ids=ADAPTER_IDS)
def test_an_error_field_in_the_result_is_still_an_error(read):
    result = read({"error": "order not found"})
    assert result.status is ResultStatus.ERROR
    assert result.error == "order not found"


@pytest.mark.parametrize("read", ADAPTERS, ids=ADAPTER_IDS)
def test_a_status_code_in_the_result_is_data(read):
    # A link checker reporting a broken page succeeded; only a failure_when contract says otherwise.
    result = read({"url": "https://shop.example.com/sale", "status_code": 404})
    assert result.status is not ResultStatus.ERROR
    assert result.content["status_code"] == 404


def _link_check(contract: dict[str, Any] | None) -> LintReport:
    trace = Trace(
        run_id="links",
        steps=[
            Message(
                Role.USER, "Check https://shop.example.com/sale; open a ticket if it is broken."
            ),
            ToolCall("c1", "check_url", {"url": "https://shop.example.com/sale"}),
            ToolResult("c1", {"url": "https://shop.example.com/sale", "status_code": 404}),
            ToolCall("c2", "create_ticket", {"url": "https://shop.example.com/sale"}),
            ToolResult("c2", {"ticket": "WEB-1"}, ResultStatus.OK),
        ],
    )
    tools = {"check_url": {}, "create_ticket": {"metadata": {"side_effecting": True}}}
    if contract:
        tools["check_url"] = {"metadata": {"failure_when": contract}}
    return lint_trace(trace, default_rules(), ToolRegistry.from_dict({"tools": tools}))


def test_a_status_code_in_the_result_is_a_candidate_until_a_contract_says_otherwise():
    # Opening a ticket for the broken link is the agent doing its job: 0.9 failed CI here (R2b).
    report = _link_check(None)
    assert report.exit_code == 0
    assert _rules(report, ConfidenceTier.CANDIDATE) == ["R2a"]
    (candidate,) = [f for f in report.active_findings if f.rule == "R2a"]
    assert "status_code=404" in candidate.summary

    # Declared, the 404 becomes a fact (here: for a tool whose 404 means it failed).
    declared = _link_check({"pointer": "/status_code", "in": [404]})
    assert "R2a" in _rules(declared, ConfidenceTier.HARD_EVENT)


# --- ToolMessage status, and HTTP statuses recorded as strings --------------------------------


def test_a_tool_message_with_status_error_is_a_structured_error():
    message = {"content": "Error: card processor down", "type": "tool", "status": "error"}
    for read in (_langfuse_result, _langsmith_result):
        result = read({"output": message} if read is _langsmith_result else message)
        assert result.status is ResultStatus.ERROR
        assert result.content == "Error: card processor down"


def test_a_tool_message_with_status_success_says_nothing_about_the_result():
    # status "success" only means the tool returned rather than raised: a deploy's own result can
    # still be a failure, so it must not read as verified success (R2a would stop disclosing it).
    message = {"content": json.dumps({"deployed": True}), "type": "tool", "status": "success"}
    assert _langfuse_result(message).status is ResultStatus.UNKNOWN


def test_an_http_status_recorded_as_a_string_is_read_not_crashed_on():
    native = Trace.from_dict(
        {
            "run_id": "s",
            "steps": [
                {"type": "tool_call", "call_id": "c1", "name": "get_order", "args": {}},
                {
                    "type": "tool_result",
                    "call_id": "c1",
                    "content": "not found",
                    "http_status": "404",
                },
            ],
        }
    )
    assert native.tool_results()[0].http_status == 404
    assert "R2a" in _rules(lint_trace(native, default_rules()), ConfidenceTier.HARD_EVENT)

    messages = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "get_order"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "not found", "http_status": "404"},
    ]
    result = from_openai_messages(messages).tool_results()[0]
    assert (result.http_status, result.status) == (404, ResultStatus.ERROR)


def test_a_langsmith_run_status_is_the_structured_status():
    run = {"id": "r", "run_type": "tool", "name": "t", "inputs": {}, "outputs": {"output": "ok"}}
    assert (
        from_langsmith_run({**run, "status": "success"}).tool_results()[0].status is ResultStatus.OK
    )
    assert (
        from_langsmith_run({**run, "status": "error"}).tool_results()[0].status
        is ResultStatus.ERROR
    )
