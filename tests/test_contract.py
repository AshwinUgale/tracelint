"""`tracelint init` — discover a starter tools.json from a trace."""

from __future__ import annotations

import json
from pathlib import Path

from tracelint.adapters.otel import from_otel_spans
from tracelint.cli import main
from tracelint.contract import discover_contract
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolCall, Trace

TRACES = Path(__file__).parents[1] / "examples" / "traces"


def _otel_trace(name: str) -> Trace:
    return from_otel_spans(json.loads((TRACES / f"{name}_trace.json").read_text(encoding="utf-8")))


def test_discovers_schema_from_real_traces():
    # smolagents/langgraph/crewai/langflow all carry a tool schema (tool.parameters or llm.tools).
    for fw, tool_name in [
        ("smolagents", "refund_order"),
        ("langgraph", "refund_order"),
        ("crewai", "refund_order"),
        ("langflow", "fetch_content"),
    ]:
        draft = discover_contract([_otel_trace(fw)])
        assert tool_name in draft.tools, fw
        schema = draft.tools[tool_name]["schema"]
        assert isinstance(schema, dict) and schema.get("type") == "object", fw
        assert tool_name in draft.with_schema, fw


def test_behavior_fields_are_null_placeholders():
    draft = discover_contract([_otel_trace("smolagents")])
    meta = draft.tools["refund_order"]["metadata"]
    assert meta == {"side_effecting": None, "idempotent": None, "failure_when": None}


def test_framework_internal_tools_are_skipped():
    # smolagents' trace calls final_answer; it must not appear in the contract (matches R7).
    draft = discover_contract([_otel_trace("smolagents")])
    assert "final_answer" not in draft.tools
    assert "final_answer" in draft.skipped_internal


def test_draft_roundtrips_as_a_valid_registry():
    draft = discover_contract([_otel_trace("langgraph")])
    registry = ToolRegistry.from_dict(draft.to_dict())  # must not raise
    spec = registry.get("refund_order")
    assert spec is not None and spec.schema is not None
    # null behavior fields load as the conservative defaults.
    assert spec.metadata.side_effecting is False
    assert spec.metadata.idempotent is False
    assert spec.metadata.failure_when is None


def test_schema_inferred_from_observed_args_when_undeclared():
    # No declared schema, but args were observed → infer an object schema, marked for review.
    trace = Trace(
        run_id="t",
        steps=[
            ToolCall("c1", "charge", {"amount": 49.99, "currency": "USD"}),
            ToolCall("c2", "charge", {"amount": 10, "currency": "USD"}),  # int vs float → drop type
        ],
    )
    draft = discover_contract([trace])
    assert draft.inferred_schema == ["charge"]
    schema = draft.tools["charge"]["schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["currency"] == {"type": "string"}
    assert schema["properties"]["amount"] == {}  # inconsistent type → left unconstrained
    assert "$comment" in schema  # marked as inferred


def test_no_args_tool_has_null_schema_for_review():
    trace = Trace(run_id="t", steps=[ToolCall("c1", "ping", {})])  # no schema, no args
    draft = discover_contract([trace])
    assert draft.tools["ping"]["schema"] is None
    assert draft.no_schema == ["ping"]


def test_todo_present_and_ignored_on_load():
    trace = Trace(run_id="t", steps=[ToolCall("c1", "charge", {"amount": 5})])
    draft = discover_contract([trace])
    assert draft.tools["charge"]["_todo"]  # self-documenting TODOs live in the JSON
    # _todo and the top-level _comment must not break loading as a registry.
    registry = ToolRegistry.from_dict(draft.to_dict())
    assert registry.get("charge") is not None


def test_cli_init_prints_valid_contract(capsys):
    code = main(["init", str(TRACES / "crewai_trace.json"), "--format", "openinference"])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # stdout is the contract JSON (summary goes to stderr)
    assert "refund_order" in payload["tools"]


def test_cli_init_writes_output_file(tmp_path, capsys):
    out = tmp_path / "tools.json"
    code = main(
        ["init", str(TRACES / "smolagents_trace.json"), "--format", "openinference", "-o", str(out)]
    )
    assert code == 0
    contract = json.loads(out.read_text(encoding="utf-8"))
    assert "refund_order" in contract["tools"]
    # a written file prints the review summary to stdout
    assert "REVIEW behavior" in capsys.readouterr().out
