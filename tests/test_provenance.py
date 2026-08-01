"""Phase 3 — the provenance graph + derivability test (spec §II.4; learning-doc 02 §2)."""

from __future__ import annotations

from tracelint.provenance import SourceType, build_provenance
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult, build_trace


def _graph(steps, up_to):
    return build_provenance(build_trace("r", steps).steps, up_to)


def test_value_from_user_message_is_derivable():
    steps = [Message(Role.USER, "cancel order 4521 please"), ToolCall("c1", "cancel", {})]
    g = _graph(steps, up_to=1)
    assert g.derive("4521").derivable is True
    assert g.derive("4521").operation in {"substring", "digits"}


def test_value_from_tool_result_is_derivable():
    steps = [
        Message(Role.USER, "look it up"),
        ToolCall("c1", "lookup", {}),
        ToolResult("c1", {"customer_id": "CUST-99"}, status=ResultStatus.OK),
        ToolCall("c2", "notify", {}),
    ]
    g = _graph(steps, up_to=3)
    assert g.derive("CUST-99").derivable is True
    assert g.derive("CUST-99").source_type == "tool"


def test_digit_reformat_is_derivable():
    steps = [Message(Role.USER, "the total was 1234.56 dollars"), ToolCall("c1", "x", {})]
    g = _graph(steps, up_to=1)
    assert g.derive("1,234.56").derivable is True  # comma reformat still matches by digits


def test_unseen_value_is_not_derivable():
    steps = [Message(Role.USER, "book me a flight"), ToolCall("c1", "send", {})]
    g = _graph(steps, up_to=1)
    assert g.derive("CONF-4821").derivable is False


def test_model_generated_thought_is_not_a_source():
    # A value that only appears in an assistant turn must NOT become derivable (no laundering).
    steps = [
        Message(Role.USER, "hi"),
        Message(Role.ASSISTANT, "I'll use confirmation CONF-4821"),
        ToolCall("c1", "send", {}),
    ]
    g = _graph(steps, up_to=2)
    assert g.derive("CONF-4821").derivable is False


def test_only_prior_steps_are_in_scope():
    steps = build_trace(
        "r",
        [
            ToolCall("c1", "a", {}),
            ToolResult("c1", {"token": "ABC-123456"}, status=ResultStatus.OK),
            ToolCall("c2", "b", {}),
        ],
    ).steps
    # Before step 1, the result at step 1 is not yet observed.
    assert build_provenance(steps, up_to_index=1).derive("ABC-123456").derivable is False
    # By step 2 it is.
    assert build_provenance(steps, up_to_index=2).derive("ABC-123456").derivable is True


def test_concatenation_transform():
    steps = [
        Message(Role.USER, "first is alpha and second is bravo"),
        ToolCall("c1", "x", {}),
    ]
    g = build_provenance(build_trace("r", steps).steps, up_to_index=1)
    g.add_value("alpha", SourceType.USER, 0)
    g.add_value("bravo", SourceType.USER, 0)
    assert g.derive("alpha-bravo").derivable is True


def test_trivial_short_value_is_not_flagged():
    g = _graph([Message(Role.USER, "hi"), ToolCall("c1", "x", {})], up_to=1)
    assert g.derive("f").derivable is True  # too short to be a fabrication
