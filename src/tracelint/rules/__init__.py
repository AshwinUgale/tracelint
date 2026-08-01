"""Deterministic rules and the fail-closed driver that runs them.

Phase 0 ships the rule *contract* (:class:`Rule`) and the driver (:func:`lint_trace`). The
concrete rules (R1 schema violation, R2 error handling, R3 hallucinated arg, R4 loop, R5
redundant call) arrive in later phases and register here.
"""

from __future__ import annotations

from tracelint.rules.base import Rule, lint_trace
from tracelint.rules.schema_violation import SchemaViolationRule


def default_rules() -> list[Rule]:
    """The rules run by ``tracelint check`` when no subset is named. Grows each phase."""
    return [SchemaViolationRule()]


__all__ = ["Rule", "lint_trace", "SchemaViolationRule", "default_rules"]
