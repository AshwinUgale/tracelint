"""Phase 5 — the seeded fault injector (spec §II.6; learning-doc 03 §2)."""

from __future__ import annotations

import json

from tracelint import ConfidenceTier, FaultInjector, FaultType, RandomInjection, TargetedInjection
from tracelint.agent import ReActAgent, ScriptedLLM, build_demo_toolset, final, tool
from tracelint.injection import apply_fault
from tracelint.rules import ToolErrorEventRule
from tracelint.trace import ResultStatus, ToolCall

CALL = ToolCall("c1", "reserve", {"x": 1})


def test_each_fault_renders_correctly():
    assert apply_fault(FaultType.TIMEOUT, CALL).status is ResultStatus.ERROR
    err = apply_fault(FaultType.ERROR, CALL)
    assert err.http_status == 500 and err.status is ResultStatus.ERROR
    assert apply_fault(FaultType.RATE_LIMIT, CALL).http_status == 429
    empty = apply_fault(FaultType.EMPTY, CALL)
    assert empty.content == [] and empty.status is ResultStatus.OK
    wrong = apply_fault(FaultType.WRONG_SCHEMA, CALL)
    assert wrong.content == {"unexpected_field": True} and wrong.status is ResultStatus.OK


def test_malformed_json_does_not_parse_but_looks_ok():
    result = apply_fault(FaultType.MALFORMED_JSON, CALL)
    assert result.status is ResultStatus.OK  # silent — status says success
    try:
        json.loads(result.content)
        parsed = True
    except (json.JSONDecodeError, ValueError):
        parsed = False
    assert parsed is False


def test_truncated_uses_the_original():
    from tracelint.trace import ToolResult

    original = ToolResult("c1", "abcdefgh", status=ResultStatus.OK)
    truncated = apply_fault(FaultType.TRUNCATED, CALL, original)
    assert truncated.content == "abcd"


def test_targeted_injection_hits_the_named_call_and_tags_it():
    toolset = build_demo_toolset()
    injector = FaultInjector(toolset, TargetedInjection(FaultType.ERROR, tool="get_order_status"))
    result = injector.execute(ToolCall("c1", "get_order_status", {"order_id": "4521"}))
    assert result.status is ResultStatus.ERROR and result.http_status == 500
    assert result.meta is not None and result.meta.injected is True
    assert result.meta.fault_injection_id.startswith("error@")


def test_non_targeted_calls_pass_through_unchanged():
    toolset = build_demo_toolset()
    injector = FaultInjector(toolset, TargetedInjection(FaultType.ERROR, tool="cancel_order"))
    # get_order_status is not the target → real result.
    result = injector.execute(ToolCall("c1", "get_order_status", {"order_id": "4521"}))
    assert result.status is ResultStatus.OK
    assert result.meta is None or not result.meta.injected


def test_targeted_occurrence():
    toolset = build_demo_toolset()
    injector = FaultInjector(
        toolset, TargetedInjection(FaultType.ERROR, tool="get_order_status", occurrence=2)
    )
    first = injector.execute(ToolCall("c1", "get_order_status", {"order_id": "4521"}))
    second = injector.execute(ToolCall("c2", "get_order_status", {"order_id": "4521"}))
    assert first.status is ResultStatus.OK  # first call untouched
    assert second.status is ResultStatus.ERROR  # second call injected


def test_random_injection_is_reproducible_with_a_seed():
    plan = RandomInjection([FaultType.ERROR, FaultType.TIMEOUT], rate=0.5)
    calls = [ToolCall(f"c{i}", "t", {"i": i}) for i in range(20)]

    def decisions(seed):
        rng_injector = FaultInjector(build_demo_toolset(), plan, seed=seed)
        out = []
        for c in calls:
            rng_injector._global += 1
            out.append(plan.decide(c, rng_injector._global, 1, rng_injector.rng))
        return out

    assert decisions(42) == decisions(42)  # same seed → same fault pattern


def test_injector_is_drop_in_for_the_agent():
    toolset = build_demo_toolset()
    injector = FaultInjector(toolset, TargetedInjection(FaultType.ERROR, tool="get_order_status"))
    agent = ReActAgent(
        ScriptedLLM([tool("get_order_status", {"order_id": "4521"}), final("done")]), injector
    )
    trace = agent.run("check 4521")
    from tracelint import lint_trace

    report = lint_trace(trace, [ToolErrorEventRule()], toolset.to_registry())
    assert any(f.tier is ConfidenceTier.HARD_EVENT for f in report.active_findings)
    # The injected step is tagged for reproducibility.
    injected = [r for r in trace.tool_results() if r.meta and r.meta.injected]
    assert len(injected) == 1
