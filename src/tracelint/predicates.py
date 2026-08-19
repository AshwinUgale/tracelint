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
    {"pointer": "", "contains": "Error:"}                     # free text contains a substring
    {"pointer": "", "matches": "^(Error|Failed)\\b"}          # free text matches a regex

The pointer is an RFC 6901 JSON Pointer into the result content (``""`` is the whole result). A
predicate with a non-empty pointer and no condition defaults to ``exists`` (present ⇒ failure). The
``contains`` / ``matches`` modes exist for tools that report failure as **free text** — the many MCP
tools that return a plain ``"Error: ..."`` string over a 200 with no error status; ``matches`` is a
regular expression, ``contains`` a substring, both tested against the string form of the value. This
is the same "declare per-tool semantics" model as ``metadata.side_effecting`` and per-field
``x-value-origin`` — never inferred from a name. The declaration lives in the *operator's*
``tools.json``, so it works for third-party tools whose authors declare nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_UNSET = object()  # distinguishes "equals not configured" from "equals: null"


def _as_text(value: Any) -> str:
    """String form of a resolved value for ``contains`` / ``matches`` (JSON for containers)."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, default=str)


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
    contains: str | None = None
    pattern: str | None = None  # the JSON ``matches`` key — a regular expression
    exists: bool = False

    def _has_explicit_condition(self) -> bool:
        return bool(
            self.in_values
            or self.equals is not _UNSET
            or self.contains is not None
            or self.pattern is not None
        )

    @classmethod
    def from_dict(cls, data: Any) -> FailurePredicate | None:
        if not isinstance(data, dict) or "pointer" not in data:
            return None  # pointer is required (may be "" for the whole result)
        pointer = data.get("pointer")
        if not isinstance(pointer, str):
            return None
        raw_in = data.get("in")
        in_values = tuple(raw_in) if isinstance(raw_in, list) else ()
        equals = data["equals"] if "equals" in data else _UNSET
        contains = data["contains"] if isinstance(data.get("contains"), str) else None
        pattern = data["matches"] if isinstance(data.get("matches"), str) else None
        exists = bool(data.get("exists", False))
        pred = cls(
            pointer=pointer,
            in_values=in_values,
            equals=equals,
            contains=contains,
            pattern=pattern,
            exists=exists,
        )
        if not (pred._has_explicit_condition() or exists):
            # A *non-empty* pointer with no condition means "failure iff this path is present".
            # An empty pointer with nothing to test is meaningless — reject it.
            if not pointer:
                return None
            pred = cls(pointer=pointer, exists=True)
        return pred

    def matches(self, content: Any) -> bool:
        """True iff ``content`` satisfies this failure predicate."""
        value, found = resolve_pointer(content, self.pointer)
        if not found:
            return False
        if self.in_values and value in self.in_values:
            return True
        if self.equals is not _UNSET and value == self.equals:
            return True
        if self.contains is not None and self.contains in _as_text(value):
            return True
        if self.pattern is not None:
            try:
                if re.search(self.pattern, _as_text(value)):
                    return True
            except re.error:
                pass  # a malformed pattern never matches (config error, fails safe)
        # ``exists`` fires only when it is the sole condition (pointer present ⇒ failure).
        return bool(self.exists and not self._has_explicit_condition())

    def describe(self, content: Any) -> str:
        """A short evidence string naming the matched path and value."""
        value, _ = resolve_pointer(content, self.pointer)
        text = repr(value)
        if len(text) > 120:
            text = text[:117] + "..."
        return f"{self.pointer or '(result)'}={text}"
