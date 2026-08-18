"""Declared per-tool failure predicates (spec §II.5, R2; review follow-up).

Structured error detection reads only what the transport/instrumentation declared — an explicit
error status, an ``http_status >= 400``, or an ``error`` field (see
:func:`tracelint.signatures.is_structured_error`). But a large class of real failures arrives as a
*transport success carrying a domain failure in the body* — a declined charge returned as HTTP 200
with ``{"status": "declined"}``. Nothing in the payload is inherently "an error"; only the tool's
own contract knows that ``status == "declined"`` means it failed.

A ``failure_when`` predicate lets a tool **declare** that contract once, in ``tools.json``, keeping
the decision structural (deterministic, no guessing) rather than pushing it onto a model:

    {"pointer": "/status", "in": ["declined", "failed"]}     # failure iff /status ∈ {...}
    {"pointer": "/ok", "equals": false}                       # failure iff /ok == false
    {"pointer": "/error_code", "exists": true}                # failure iff /error_code is present

The pointer is an RFC 6901 JSON Pointer into the result content. A predicate with a pointer but no
condition defaults to ``exists`` (present ⇒ failure). This is the same "declare per-tool semantics"
model as ``metadata.side_effecting`` and per-field ``x-value-origin`` — never inferred from a name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_UNSET = object()  # distinguishes "equals not configured" from "equals: null"


def resolve_pointer(content: Any, pointer: str) -> tuple[Any, bool]:
    """Resolve an RFC 6901 JSON Pointer against ``content``. Return ``(value, found)``.

    ``""`` (or ``"/"``) is the whole document. A pointer without a leading ``/`` is treated as a
    single top-level key, a small convenience for the common ``"status"`` case.
    """
    if pointer in ("", "/"):
        return content, True
    parts = (
        [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
        if pointer.startswith("/")
        else [pointer]
    )
    cur = content
    for part in parts:
        if isinstance(cur, dict):
            if part not in cur:
                return None, False
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return None, False
            if not 0 <= idx < len(cur):
                return None, False
            cur = cur[idx]
        else:
            return None, False
    return cur, True


@dataclass(frozen=True)
class FailurePredicate:
    """A structural test for whether a tool's result represents a domain failure."""

    pointer: str
    in_values: tuple[Any, ...] = ()
    equals: Any = _UNSET
    exists: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> FailurePredicate | None:
        if not isinstance(data, dict):
            return None
        pointer = data.get("pointer")
        if not isinstance(pointer, str) or not pointer:
            return None
        raw_in = data.get("in")
        in_values = tuple(raw_in) if isinstance(raw_in, list) else ()
        equals = data["equals"] if "equals" in data else _UNSET
        exists = bool(data.get("exists", False))
        # A pointer with no stated condition means "failure iff this path is present".
        if not in_values and equals is _UNSET and not exists:
            exists = True
        return cls(pointer=pointer, in_values=in_values, equals=equals, exists=exists)

    def matches(self, content: Any) -> bool:
        """True iff ``content`` satisfies this failure predicate."""
        value, found = resolve_pointer(content, self.pointer)
        if not found:
            return False
        if self.in_values and value in self.in_values:
            return True
        if self.equals is not _UNSET and value == self.equals:
            return True
        return bool(self.exists and not self.in_values and self.equals is _UNSET)

    def describe(self, content: Any) -> str:
        """A short evidence string naming the matched path and value."""
        value, _ = resolve_pointer(content, self.pointer)
        return f"{self.pointer}={value!r}"
