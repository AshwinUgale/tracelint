"""Deterministic rules and the fail-closed driver that runs them.

Phase 0 ships the rule *contract* (:class:`Rule`) and the driver (:func:`lint_trace`). The
concrete rules (R1 schema violation, R2 error handling, R3 hallucinated arg, R4 loop, R5
redundant call) arrive in later phases and register here.
"""

from __future__ import annotations

from tracelint.rules.base import Rule, lint_trace

__all__ = ["Rule", "lint_trace"]
