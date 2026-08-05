"""The real-trace generator's tool instrumentation — validated offline with a fake decorator."""

from __future__ import annotations

from examples.langfuse_generate_and_lint import _instrument_toolset
from examples.real_agent import build_support_toolset
from tracelint.trace import ResultStatus, ToolCall


def _identity_observe(*_args, **_kwargs):
    """Stand in for ``langfuse.observe``: a decorator that returns the function unchanged."""

    def decorator(fn):
        return fn

    return decorator


def test_instrument_toolset_wraps_every_tool_and_preserves_behavior():
    toolset = build_support_toolset()
    names_before = set(toolset.names())

    _instrument_toolset(toolset, _identity_observe)

    # Wrapping must not change which tools exist or how they behave.
    assert set(toolset.names()) == names_before

    ok = toolset.execute(ToolCall(call_id="c1", name="lookup_order", args={"order_id": "A100"}))
    assert ok.status is ResultStatus.OK

    # Z999 does not exist -> the tool still raises ToolError, surfaced as an error result.
    err = toolset.execute(ToolCall(call_id="c2", name="lookup_order", args={"order_id": "Z999"}))
    assert err.status is ResultStatus.ERROR
    assert err.http_status == 404


def test_instrument_toolset_falls_back_when_as_type_unsupported():
    calls: list[dict] = []

    def observe_without_as_type(*, name, **kwargs):
        if "as_type" in kwargs:
            raise TypeError("as_type not supported on this SDK")
        calls.append({"name": name})

        def decorator(fn):
            return fn

        return decorator

    toolset = build_support_toolset()
    _instrument_toolset(toolset, observe_without_as_type)
    # Every tool was still wrapped via the no-as_type fallback path.
    assert {c["name"] for c in calls} == set(toolset.names())
