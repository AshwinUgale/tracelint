"""The `trace_capture` pytest fixture captures a run, lints it, and fails on a hard defect.

Exercised with pytest's own `pytester`, which runs a throwaway test that enables the plugin and uses
the fixture — the same way a user would. Everything is offline: the inner tests use `framework=None`
and emit spans on the handle's tracer, so no framework, API key, or network is involved (the
per-framework instrumentor wiring is covered by the real-trace fixtures in test_framework_examples).
"""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk")

pytest_plugins = ["pytester"]

# Helpers the inner tests import to emit OpenInference spans on the capture's tracer: a TOOL span
# (what a tool received) and an LLM span whose output is one tool call (what the model emitted).
_EMIT = '''
import json

def emit_tool_span(tracer, name, input_value, output):
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", name)
        span.set_attribute("input.value", input_value)
        span.set_attribute("output.value", json.dumps(output))

def emit_model_tool_call(tracer, name, arguments):
    with tracer.start_as_current_span("llm") as span:
        span.set_attribute("openinference.span.kind", "LLM")
        call = "llm.output_messages.0.message.tool_calls.0.tool_call."
        span.set_attribute("llm.output_messages.0.message.role", "assistant")
        span.set_attribute(call + "function.name", name)
        span.set_attribute(call + "function.arguments", arguments)
'''


def _conftest() -> str:
    return 'pytest_plugins = ["tracelint.pytest_plugin"]'


def test_clean_run_passes_and_exposes_the_report(pytester):
    pytester.makeconftest(_conftest())
    pytester.makepyfile(
        emit=_EMIT,
        test_clean='''
        import json
        from emit import emit_tool_span

        def test_agent_is_clean(trace_capture):
            with trace_capture() as cap:                       # framework=None: manual tracer
                emit_tool_span(cap.tracer, "get_order",
                               json.dumps({"order_id": "A100"}), {"status": "ok"})
            # after the block the trace has been linted and the report is available
            assert cap.report is not None
            assert cap.report.exit_code == 0
        ''',
    )
    pytester.runpytest().assert_outcomes(passed=1)


def test_hard_defect_fails_the_test(pytester):
    pytester.makeconftest(_conftest())
    pytester.makepyfile(
        emit=_EMIT,
        test_defect='''
        from emit import emit_model_tool_call

        def test_agent_has_a_defect(trace_capture):
            with trace_capture() as cap:                       # framework=None: manual tracer
                # The model emits a tool call whose arguments are not valid JSON -> R6 (hard).
                emit_model_tool_call(cap.tracer, "get_order", "{ not valid json")
        ''',
    )
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*hard defect*"])  # the assertion message names it


def test_assert_clean_false_lets_the_test_inspect_instead(pytester):
    pytester.makeconftest(_conftest())
    pytester.makepyfile(
        emit=_EMIT,
        test_inspect='''
        from emit import emit_model_tool_call

        def test_agent_inspected(trace_capture):
            with trace_capture(assert_clean=False) as cap:     # do not auto-fail
                emit_model_tool_call(cap.tracer, "get_order", "{ not valid json")
            # the hard defect did NOT fail the test; the report is ours to assert on
            assert cap.report.has_hard_defect
            assert cap.report.exit_code == 2
        ''',
    )
    pytester.runpytest().assert_outcomes(passed=1)
