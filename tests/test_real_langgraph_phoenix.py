"""Real current-LangGraph traces through Phoenix — regression fixtures for three adapter gaps.

Both fixtures are one real gpt-4o-mini run of a LangGraph 1.2 release agent (instrumented with
openinference-instrumentation-langchain 0.1.76). Its pipeline tool returned a Jenkins ``UNSTABLE``
build, and the agent deployed that build to production anyway:

- ``langgraph_phoenix_trace.json`` — the spans exactly as ``phoenix.client.Client().spans
  .get_spans_dataframe(...)`` returns them, with message attributes *unflattened* into lists.
- ``langgraph_capture_trace.json`` — the same agent recorded by ``tracelint.capture`` (flat keys).

On tracelint 0.8.0 the Phoenix shape produced a false R6 hard defect (exit 2 on a valid call) and
lost the user's request; on both shapes the LangChain ``ToolMessage`` envelope hid the tool's
result, so the failed-pipeline -> production-deploy defect (R2b) was missed; and a multi-run
Phoenix export was linted as one merged trace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelint import (
    ToolRegistry,
    default_rules,
    lint_otel_trace,
    lint_otel_traces,
    lint_trace,
    load_source,
)
from tracelint.adapters.otel import from_otel_spans
from tracelint.cli import main
from tracelint.findings import ConfidenceTier
from tracelint.trace import ResultStatus, Role

TRACES = Path(__file__).resolve().parent.parent / "examples" / "traces"
PHOENIX = TRACES / "langgraph_phoenix_trace.json"
CAPTURE = TRACES / "langgraph_capture_trace.json"

# The operator's contract: Jenkins' non-success results are failures; deploy has side effects.
CONTRACT = {
    "tools": {
        "run_release_pipeline": {
            "metadata": {
                "failure_when": {"pointer": "/result", "in": ["FAILURE", "UNSTABLE", "ABORTED"]}
            }
        },
        "deploy": {
            "metadata": {
                "side_effecting": True,
                "failure_when": {"pointer": "/status", "in": ["failed", "rolled_back"]},
            }
        },
    }
}
REGISTRY = ToolRegistry.from_dict(CONTRACT)


def _spans(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rules(report, tier: ConfidenceTier | None = None) -> list[str]:
    return sorted(f.rule for f in report.active_findings if tier is None or f.tier is tier)


def test_phoenix_shape_valid_calls_do_not_fail_ci():
    report = lint_otel_trace(_spans(PHOENIX))
    assert report.exit_code == 0
    assert "R6" not in _rules(report)  # a false hard defect on 0.8.0
    assert "R3" not in _rules(report)  # the user's request is now read from the nested messages


def test_phoenix_shape_reads_nested_messages_and_recovers_lossy_args():
    trace = from_otel_spans(_spans(PHOENIX))
    users = [s for s in trace.steps if getattr(s, "role", None) is Role.USER]
    assert users and "to production" in users[0].content
    pipeline = trace.tool_calls()[0]
    # LangChain records this single-argument TOOL input as the bare scalar "4.12.0"; the real
    # arguments come from the LLM span's (unflattened) tool_calls.
    assert pipeline.name == "run_release_pipeline"
    assert pipeline.args == {"version": "4.12.0"}
    assert pipeline.raw_text is None


@pytest.mark.parametrize("path", [PHOENIX, CAPTURE], ids=["phoenix", "capture"])
def test_failed_pipeline_to_production_deploy_is_a_hard_defect(path):
    report = lint_otel_trace(_spans(path), registry=REGISTRY)
    assert report.exit_code == 2
    assert _rules(report, ConfidenceTier.HARD_DEFECT) == ["R2b"]
    assert "R2a" in _rules(report, ConfidenceTier.HARD_EVENT)


@pytest.mark.parametrize("path", [PHOENIX, CAPTURE], ids=["phoenix", "capture"])
def test_tool_message_envelope_is_unwrapped(path):
    trace = from_otel_spans(_spans(path))
    result = trace.result_for(trace.tool_calls()[0])
    assert result.content["result"] == "UNSTABLE"  # the tool's payload, not the envelope
    assert result.content["build_id"] == "b-4120-7f3a"


def test_tool_message_error_status_is_a_structured_error():
    envelope = {
        "type": "tool",
        "data": {"content": "Error: permission denied", "status": "error", "type": "tool"},
    }
    span = {
        "trace_id": "t1",
        "span_id": "s1",
        "start_time": 1,
        "status_code": "OK",
        "attributes": {
            "openinference.span.kind": "TOOL",
            "tool.name": "deploy",
            "input.value": json.dumps({"build_id": "b-1"}),
            "output.value": json.dumps(envelope),
        },
    }
    trace = from_otel_spans([span])
    result = trace.result_for(trace.tool_calls()[0])
    assert result.status is ResultStatus.ERROR
    assert result.content == "Error: permission denied"


def _two_runs() -> list[dict]:
    first = _spans(PHOENIX)
    second = [dict(s, **{"context.trace_id": "second-run"}) for s in first]
    return first + second


def test_multi_run_phoenix_export_is_split_per_trace(tmp_path):
    export = tmp_path / "project_export.json"
    export.write_text(json.dumps(_two_runs()), encoding="utf-8")
    assert len(load_source(export, "openinference")) == 2
    reports = lint_otel_traces(_two_runs(), registry=REGISTRY)
    assert len(reports) == 2
    assert all(r.exit_code == 2 for r in reports)


def test_spans_from_several_traces_are_rejected_not_merged():
    with pytest.raises(ValueError, match="different traces"):
        lint_otel_trace(_two_runs())


def _jsonl(spans: list[dict]) -> str:
    return "".join(f"{json.dumps(span)}\n" for span in spans)


def test_span_per_line_phoenix_export_finds_the_defect_in_each_run(tmp_path):
    # `spans.to_json("spans.jsonl", orient="records", lines=True)`: one span per line, two runs.
    # 0.9 linted every line as its own one-span trace, so the failed-pipeline -> deploy defect
    # (which spans steps) could never be seen.
    export = tmp_path / "spans.jsonl"
    export.write_text(_jsonl(_two_runs()), encoding="utf-8")
    traces = load_source(export, "openinference")
    reports = [lint_trace(trace, default_rules(), REGISTRY) for trace in traces]
    assert len(reports) == 2
    assert all(_rules(r, ConfidenceTier.HARD_DEFECT) == ["R2b"] for r in reports)


def test_readme_ci_command_on_jsonl_fails_the_build(tmp_path, capsys):
    # The README's CI step (`tracelint check traces/*.jsonl --format openinference --tools
    # tools.json`), over a run whose spans a rotating exporter split across two files.
    spans = _spans(PHOENIX)
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "a.jsonl").write_text(_jsonl(spans[: len(spans) // 2]), encoding="utf-8")
    (traces / "b.jsonl").write_text(_jsonl(spans[len(spans) // 2 :]), encoding="utf-8")
    tools = tmp_path / "tools.json"
    tools.write_text(json.dumps(CONTRACT), encoding="utf-8")
    sarif = tmp_path / "tracelint.sarif"

    files = sorted(str(f) for f in traces.glob("*.jsonl"))
    args = ["check", *files, "--format", "openinference", "--tools", str(tools)]
    assert main([*args, "--sarif", str(sarif)]) == 2
    assert capsys.readouterr().out.count("finding(s), exit") == 1  # one run, one report
    results = json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["results"]
    uris = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in results}
    assert uris == {files[0]}
