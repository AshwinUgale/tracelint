"""Call/result signatures for loop and redundancy detection (learning-doc 02 §4).

The whole game is the granularity of a signature (02 §4): too coarse and real loops hide; too
fine (hashing a raw payload with timestamps/ids) and nothing ever looks identical, so real repeats
hide too. Two derived notions are kept, for two different questions:

- ``result_class`` — a **coarse** bucket (``error`` / ``empty`` / ``status:<state>`` / ``ok``)
  that captures whether *state advanced*. A poll advances ``status:pending → status:completed``,
  giving different classes at the advancing step — which is exactly how a legitimate poll is told
  apart from a stuck loop.
- ``result_fingerprint`` — a **fine** canonical form of the whole result, for "the identical call
  produced the identical result" (redundancy).

``normalize_args`` canonicalizes arguments and strips volatile fields (timestamps, request ids) so
semantically-identical calls compare equal.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tracelint.trace import ResultStatus, ToolResult
from tracelint.valueutil import normalize

# Result-class states that mean "still waiting" — a poll in progress, not a stuck loop.
WAITING_STATES = {"pending", "in_progress", "queued", "running", "processing", "waiting", "started"}

# Argument keys that vary run-to-run and must not make two identical calls look different.
VOLATILE_ARG_KEYS = {
    "timestamp", "ts", "time", "request_id", "requestid", "nonce",
    "idempotency_key", "trace_id", "traceid", "span_id",
}

_EMPTY_TEXT_RE = re.compile(r"^\s*(no results?|not found|none found|0 results?)\s*$", re.IGNORECASE)


def is_structured_error(result: ToolResult) -> bool:
    """True iff the result carries an unambiguous, structured error signal (shared with R2)."""
    if result.status is ResultStatus.ERROR:
        return True
    if result.http_status is not None and result.http_status >= 400:
        return True
    return result.error is not None


def looks_empty(content: Any) -> bool:
    """True for an empty result — ``None``, an empty container, or an empty-ish phrase."""
    if content is None:
        return True
    if isinstance(content, (str, list, tuple, dict)) and len(content) == 0:
        return True
    return bool(isinstance(content, str) and _EMPTY_TEXT_RE.match(content))


def normalize_args(args: dict[str, Any]) -> str:
    """Canonical, volatile-field-stripped JSON of a call's arguments."""
    filtered = {k: v for k, v in args.items() if k.lower() not in VOLATILE_ARG_KEYS}
    return json.dumps(filtered, sort_keys=True, default=str)


def result_class(result: ToolResult | None) -> str:
    """Coarse state bucket used to tell progress from no-progress."""
    if result is None:
        return "no_result"
    if is_structured_error(result):
        return "error"
    if looks_empty(result.content):
        return "empty"
    content = result.content
    if isinstance(content, dict):
        for key in ("status", "state"):
            if key in content:
                return f"status:{normalize(content[key])}"
    return "ok"


def result_fingerprint(result: ToolResult | None) -> str:
    """Fine-grained canonical form of the whole result (for identical-result detection)."""
    if result is None:
        return "no_result"
    return json.dumps(
        {"status": result.status.value, "content": result.content}, sort_keys=True, default=str
    )


def is_waiting_class(rc: str) -> bool:
    """True if a coarse ``result_class`` denotes a still-in-progress (waiting) state."""
    return rc.startswith("status:") and rc.split(":", 1)[1].strip() in WAITING_STATES
