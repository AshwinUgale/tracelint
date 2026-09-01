"""Conformance: the OpenAI chat-message reader (standard chat + ShareGPT)."""

from __future__ import annotations

from conformance._harness import check
from tracelint.adapters.openai import from_openai_messages


def test_standard_chat_with_tool_call_and_result():
    raw = [
        {"role": "user", "content": "find shoes"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "shoes"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "[]"},
        {"role": "assistant", "content": "none found"},
    ]
    check(
        from_openai_messages(raw),
        [
            ("message", "user", "find shoes"),
            ("tool_call", "search", {"q": "shoes"}),
            # A plain OpenAI tool message carries no status signal → UNKNOWN (fail-closed default).
            ("tool_result", "search", "unknown", None, None, "[]"),
            ("message", "assistant", "none found"),
        ],
    )


def test_sharegpt_from_value_shape():
    raw = [
        {"from": "human", "value": "find shoes"},
        {
            "from": "gpt",
            "value": "searching",
            "tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
        },
    ]
    check(
        from_openai_messages(raw),
        [
            ("message", "user", "find shoes"),
            ("message", "assistant", "searching"),
            ("tool_call", "search", {}),
        ],
    )
