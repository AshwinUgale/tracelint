"""Adapters normalize provider/framework trace formats into the canonical schema (spec §II.4).

The linter never reads a provider's native format directly — an adapter maps it into
``tracelint.trace.Trace`` first, so every rule is written once against the canonical vocabulary
(deep-design Trap 1). Phase 1 ships the OpenAI chat-completions adapter, which is the format the
built-in ReAct agent emits; more adapters (Anthropic, OpenInference/OTel spans) are later work.
"""

from __future__ import annotations

from tracelint.adapters.langfuse import from_langfuse_trace, observed_tool_names
from tracelint.adapters.openai import from_openai_messages, openai_tools_to_registry
from tracelint.adapters.otel import from_otel_spans

__all__ = [
    "from_openai_messages",
    "openai_tools_to_registry",
    "from_langfuse_trace",
    "observed_tool_names",
    "from_otel_spans",
]
