"""Adapter robustness — the "never crash" invariant.

A linter that throws an uncontrolled traceback on a real (if odd) export is a linter a user
removes after the first run. Every adapter entry point must therefore, for *any* input, either
return a valid canonical :class:`Trace` or raise a **controlled** error (``ValueError`` /
``TypeError``) — never an uncontrolled ``KeyError`` / ``IndexError`` / ``AttributeError`` /
``RecursionError`` that leaks the adapter's internals.

This is deliberately separate from the conformance suite: conformance pins *exact* normalization
of well-formed payloads; this pins *graceful behavior* on malformed ones. A regression here means
a real user's weird trace could crash the tool.
"""

from __future__ import annotations

import pytest

from tracelint.adapters import (
    from_langfuse_trace,
    from_langsmith_run,
    from_openai_messages,
    from_otel_spans,
)
from tracelint.cli import main
from tracelint.trace import Trace

CONTROLLED = (ValueError, TypeError)

ADAPTERS = {
    "langfuse": from_langfuse_trace,
    "otel": from_otel_spans,
    "openai": from_openai_messages,
    "langsmith": from_langsmith_run,
}

# A spread of malformed / hostile inputs any real export might approximate: wrong top-level types,
# containers of the wrong shape, non-dict items where dicts are assumed, nulls in place of values,
# and deep nesting. Every adapter is fed every payload — most are "not my format", and the point is
# that a foreign or broken payload degrades or is rejected cleanly, never crashes.
PAYLOADS: dict[str, object] = {
    "none": None,
    "empty_dict": {},
    "empty_list": [],
    "scalar_str": "not a trace",
    "scalar_int": 5,
    "list_of_none": [None, None],
    "obs_not_list": {"id": "t", "observations": "oops"},
    "obs_items_not_dict": {"id": "t", "observations": [None, 3, "x"]},
    "name_not_str": {"id": "t", "observations": [{"type": "tool", "name": 123, "input": {}}]},
    "input_not_dict": {"id": "t", "observations": [{"type": "tool", "name": "t", "input": [1, 2]}]},
    "output_null": {"id": "t", "observations": [{"type": "tool", "name": "t", "output": None}]},
    "spans_dict_not_list": {"spans": "no"},
    "span_missing_attrs": [{"span_id": "s"}],
    "span_attrs_not_dict": [{"attributes": 7}],
    "messages_items_not_dict": [1, "two", None],
    "content_typed_block_bad": [{"role": "user", "content": [{"type": "text"}]}],
    "tool_calls_item_bad": [{"role": "assistant", "tool_calls": [{"function": None}]}],
    "tool_calls_not_list": [{"role": "assistant", "tool_calls": {"x": 1}}],
    "messages_wrapper": {"messages": [{"role": "user", "content": "hi"}]},
    "sharegpt_wrapper": {"conversations": [{"from": "human", "value": "hi"}]},
    "run_no_keys": {"foo": "bar"},
    "run_child_bad": {"id": "r", "child_runs": [None, {"x": 1}]},
    "deep_nesting": {
        "id": "t",
        "observations": [{"type": "tool", "name": "t", "input": {"a": {"b": {"c": {}}}}}],
    },
}


@pytest.mark.parametrize("payload_name", list(PAYLOADS))
@pytest.mark.parametrize("adapter_name", list(ADAPTERS))
def test_adapter_returns_trace_or_controlled_error(adapter_name, payload_name):
    fn = ADAPTERS[adapter_name]
    payload = PAYLOADS[payload_name]
    try:
        result = fn(payload)
    except CONTROLLED:
        return  # a clean rejection is acceptable
    except Exception as exc:  # noqa: BLE001 — the whole point is to catch the uncontrolled ones
        pytest.fail(
            f"{adapter_name} raised uncontrolled {type(exc).__name__} on '{payload_name}': {exc}"
        )
    assert isinstance(result, Trace), (
        f"{adapter_name} returned {type(result).__name__}, not a Trace, on '{payload_name}'"
    )


# --- Positive robustness: the guards also widen what's accepted --------------------------

def test_openai_accepts_messages_wrapper():
    trace = from_openai_messages({"messages": [{"role": "user", "content": "hi"}]})
    assert any(getattr(s, "content", None) == "hi" for s in trace.steps)


def test_openai_accepts_sharegpt_wrapper():
    trace = from_openai_messages({"conversations": [{"from": "human", "value": "hi"}]})
    assert any(getattr(s, "content", None) == "hi" for s in trace.steps)


def test_openai_skips_non_dict_items_instead_of_crashing():
    trace = from_openai_messages([None, {"role": "user", "content": "real"}, 42])
    assert any(getattr(s, "content", None) == "real" for s in trace.steps)


def test_openai_rejects_scalar_with_typeerror():
    with pytest.raises(TypeError):
        from_openai_messages(5)


# --- CLI never crashes: any garbage file exits cleanly (0/2/3), never a traceback ---------

@pytest.mark.parametrize("fmt", ["native", "openai", "langfuse", "langsmith", "openinference"])
def test_cli_never_crashes_on_garbage_input(tmp_path, fmt):
    bad = tmp_path / "bad.json"
    bad.write_text('{"totally": "not a trace"}', encoding="utf-8")
    code = main(["check", str(bad), "--format", fmt, "--quiet"])
    assert code in (0, 2, 3), f"format {fmt} returned unexpected exit {code}"
