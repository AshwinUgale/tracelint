"""Phase 7 — the real-agent example, driven by a stub OpenAI client (no API key needed).

This proves the *real* path works — `OpenAILLM` parsing a chat-completions response, the ReAct loop
executing real tools, and tracelint linting the resulting trace — deterministically, with a fake
client scripted to mimic what GPT would return. It also covers `OpenAILLM.propose`.
"""

from __future__ import annotations

from types import SimpleNamespace

from examples.real_agent import run_with_llm
from tracelint import ConfidenceTier
from tracelint.agent.openai_llm import OpenAILLM
from tracelint.rules import default_rules, lint_trace

# --- a stub OpenAI client that returns scripted chat-completions responses -------------


def _tool_call(cid, name, arguments):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=arguments))


def _resp(*, tool_calls=None, content=None):
    message = SimpleNamespace(content=content, tool_calls=list(tool_calls) if tool_calls else None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeCompletions:
    def __init__(self, script):
        self._script = script
        self._i = 0

    def create(self, **_kwargs):
        resp = self._script[self._i]
        self._i += 1
        return resp


class FakeOpenAIClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=_FakeCompletions(script))


def _llm(script):
    return OpenAILLM(client=FakeOpenAIClient(script))


def test_clean_real_run_lints_clean():
    script = [
        _resp(tool_calls=[_tool_call("c1", "lookup_order", '{"order_id": "A100"}')]),
        _resp(tool_calls=[_tool_call("c2", "check_refund_eligibility", '{"order_id": "A100"}')]),
        _resp(
            tool_calls=[_tool_call("c3", "issue_refund", '{"order_id": "A100", "amount": 49.99}')]
        ),
        _resp(content="Refunded $49.99 for order A100."),
    ]
    trace, toolset = run_with_llm(_llm(script), "Refund my order A100 for the full amount.")
    assert [c.name for c in trace.tool_calls()] == [
        "lookup_order",
        "check_refund_eligibility",
        "issue_refund",
    ]
    report = lint_trace(trace, default_rules(), toolset.to_registry())
    assert report.exit_code == 0  # amount 49.99 came from the lookup → derivable, no R3


def test_invented_refund_amount_is_a_hard_defect():
    # The model issues a refund for an amount that appears nowhere (the schema marks it 'provided').
    script = [
        _resp(tool_calls=[_tool_call("c1", "lookup_order", '{"order_id": "A100"}')]),
        _resp(
            tool_calls=[_tool_call("c2", "issue_refund", '{"order_id": "A100", "amount": 999.99}')]
        ),
        _resp(content="Refunded."),
    ]
    trace, toolset = run_with_llm(_llm(script), "Refund order A100.")
    report = lint_trace(trace, default_rules(), toolset.to_registry())
    assert report.exit_code == 2
    assert any(
        f.rule == "R3" and f.tier is ConfidenceTier.HARD_DEFECT for f in report.active_findings
    )


def test_malformed_tool_arguments_are_tolerated():
    # A truncated arguments string must not crash the agent; the call is captured with empty args.
    script = [
        _resp(tool_calls=[_tool_call("c1", "lookup_order", '{"order_id": ')]),  # broken JSON
        _resp(content="Sorry, I could not parse that."),
    ]
    trace, _toolset = run_with_llm(_llm(script), "Refund something.")
    assert trace.tool_calls()[0].args == {}
    assert trace.final == "Sorry, I could not parse that."
