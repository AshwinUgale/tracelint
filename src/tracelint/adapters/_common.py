"""Shared argument normalization for the adapters — one implementation, so they cannot drift.

Every adapter turns a provider's record of a tool call into canonical arguments, and real
instrumentation records them in several lossy ways:

- wrapped in a function-call envelope — ``{"args": [...], "kwargs": {...}}`` (Langfuse
  ``@observe``, LangSmith, smolagents/OpenInference, which also adds ``sanitize_inputs_outputs``);
- as a bare value — LangChain records a single-string tool's input as ``"A100"`` instead of
  ``{"order_id": "A100"}`` (OpenInference), or as ``{"input": "A100"}`` (LangSmith);
- redacted — OpenInference replaces hidden inputs with ``"__REDACTED__"``;
- positionally, with no parameter names;
- or not at all — OTel GenAI instrumentations record tool content only when opted in.

When the real arguments cannot be recovered, the honest answer is *unknown* — never ``{}``. An
empty dict reads as "the model omitted every required field" to a schema check, and as "the same
call again" to the repeat rules, so a lossy record would fail CI on a valid run. :class:`ToolArgs`
carries that distinction: ``unavailable`` holds the reason, and the rules suppress (and say why)
instead of asserting.

Two sources are kept apart on purpose. :func:`model_call_args` parses what the **model emitted**
(an LLM ``tool_call``'s arguments): an unparseable string there is genuine evidence of a malformed
call, kept as ``raw_text`` for R6. :func:`tool_input_args` parses what a **tool received** (a TOOL
span's or observation's input): that is not the model's text, so it is never proof of a malformed
call. Broken JSON there is kept for R6 to show as a *candidate*, because an exporter truncating a
long input leaves exactly the same text.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any

# OpenInference's mask for hidden inputs/outputs (OPENINFERENCE_HIDE_INPUTS / _HIDE_OUTPUTS).
REDACTED_VALUES = frozenset({"__REDACTED__"})

# Keys of a recorded function-call envelope (smolagents adds ``sanitize_inputs_outputs`` too).
_ENVELOPE_KEYS = frozenset({"args", "kwargs"})
_ENVELOPE_ALLOWED = _ENVELOPE_KEYS | {"sanitize_inputs_outputs"}

_SNIPPET = 40


@dataclass(frozen=True)
class ToolArgs:
    """A tool call's arguments, plus why they could not be recovered when they couldn't.

    - ``args``: the argument object (``{}`` for a genuine zero-argument call).
    - ``raw_text``: argument text that could not be parsed into an object — the model's emitted
      string (R6 evidence), or a tool's recorded input that is broken JSON (``unavailable`` is then
      set too, and R6 shows it only as a candidate).
    - ``unavailable``: set when the real arguments are unknown; a human-readable reason. ``args`` is
      then ``{}`` and must not be read as "no arguments".
    """

    args: dict[str, Any]
    raw_text: str | None = None
    unavailable: str | None = None


def parse_serialized(raw: Any, *, lenient: bool = True) -> tuple[Any, bool]:
    """Parse a serialized value. Returns ``(value, parsed)``; ``parsed`` is False only for a string
    that is neither JSON nor (when ``lenient``) a Python literal.

    Several instrumentations serialize arguments with Python's ``str(dict)`` (single quotes,
    ``True``/``None``): not JSON, but a well-formed argument object, so ``lenient`` parsing falls
    back to ``ast.literal_eval`` (literals only — no code execution).
    """
    if not isinstance(raw, str):
        return raw, True
    try:
        return json.loads(raw), True
    except (json.JSONDecodeError, ValueError):
        pass
    if lenient:
        try:
            return ast.literal_eval(raw), True
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            pass
    return raw, False


def _snippet(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= _SNIPPET else text[: _SNIPPET - 1] + "…"


def unwrap_call_envelope(value: dict[str, Any]) -> ToolArgs | None:
    """Unwrap a recorded function-call envelope, or return ``None`` if ``value`` is not one.

    An envelope's keys are only ``args`` / ``kwargs`` (plus smolagents'
    ``sanitize_inputs_outputs``), with a list of positionals and a dict of keywords. A real argument
    object that merely *has* a parameter called ``args`` (``run_command(command, args)``) has other
    keys and is left alone.
    Positional dicts merge into the keywords (``func({...})`` puts the argument object in
    ``args[0]``); positional scalars carry no parameter names, so the arguments are unknown.
    """
    keys = set(value)
    if not (keys & _ENVELOPE_KEYS) or not keys <= _ENVELOPE_ALLOWED:
        return None
    positional = value.get("args", [])
    keywords = value.get("kwargs", {})
    if positional is None:
        positional = []
    if keywords is None:
        keywords = {}
    if not isinstance(positional, list) or not isinstance(keywords, dict):
        return None
    merged: dict[str, Any] = {}
    unnamed = []
    for item in positional:
        if isinstance(item, dict):
            merged.update(item)
        else:
            unnamed.append(item)
    merged.update(keywords)
    if unnamed:
        return ToolArgs(
            {},
            unavailable=(
                f"the arguments were passed positionally ({_snippet(unnamed)}) and recorded "
                "without parameter names"
            ),
        )
    return ToolArgs(merged)


def tool_input_args(raw: Any) -> ToolArgs:
    """Arguments from a record of what a **tool received** (a TOOL span's or observation's input).

    When the record is not an argument object, the arguments are unknown (``unavailable``), with the
    specific reason. A tool's received input is not the model's emitted text, so it cannot prove a
    malformed call; broken JSON is still kept as ``raw_text`` so R6 can show it as a candidate.
    """
    if raw is None:
        return ToolArgs({}, unavailable="the tool's arguments were not recorded")
    if isinstance(raw, str) and raw.strip() in REDACTED_VALUES:
        return ToolArgs(
            {},
            unavailable=(
                "the tool's input was redacted by the instrumentation (e.g. "
                "OPENINFERENCE_HIDE_INPUTS)"
            ),
        )
    value, parsed = parse_serialized(raw)
    if not parsed and raw.lstrip()[:1] in ("{", "["):
        return ToolArgs(
            {},
            raw_text=raw,
            unavailable=(
                f"the tool's recorded input is not valid JSON ({_snippet(raw)}), and the model's "
                "own call is not in the trace to tell a malformed call from a truncated record"
            ),
        )
    if isinstance(value, dict):
        envelope = unwrap_call_envelope(value)
        if envelope is not None:
            return envelope
        if set(value) == {"input"}:
            inner = value["input"]
            if isinstance(inner, dict):
                return ToolArgs(dict(inner))
            return ToolArgs(
                {},
                unavailable=(
                    f"the tool's input was recorded as a single bare value ({_snippet(inner)}), "
                    "not an argument object (LangChain's string-tool convention)"
                ),
            )
        return ToolArgs(dict(value))
    return ToolArgs(
        {},
        unavailable=(
            f"the tool's input was recorded as a bare value ({_snippet(value)}), not an argument "
            "object"
        ),
    )


def model_call_args(raw: Any, *, lenient: bool = True) -> ToolArgs:
    """Arguments the **model emitted** for a tool call (an LLM ``tool_call``'s ``arguments``).

    An empty or missing value is a genuine zero-argument call: OpenInference omits
    ``tool_call.function.arguments`` when the model's argument object is empty. A string that does
    not parse into an object is kept as ``raw_text`` — real evidence of a malformed call (R6).
    """
    if raw is None or raw == "":
        return ToolArgs({})
    if isinstance(raw, dict):
        return ToolArgs(dict(raw))
    if isinstance(raw, str):
        value, parsed = parse_serialized(raw, lenient=lenient)
        if parsed and isinstance(value, dict):
            return ToolArgs(dict(value))
        return ToolArgs({}, raw_text=raw)
    return ToolArgs({}, raw_text=str(raw))
