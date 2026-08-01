"""Deterministic rules and the fail-closed driver that runs them.

Phase 0 ships the rule *contract* (:class:`Rule`) and the driver (:func:`lint_trace`). The
concrete rules (R1 schema violation, R2 error handling, R3 hallucinated arg, R4 loop, R5
redundant call) arrive in later phases and register here.
"""

from __future__ import annotations

from tracelint.rules.base import Rule, lint_trace
from tracelint.rules.error_handling import ErrorHandlingRule, ToolErrorEventRule
from tracelint.rules.schema_violation import SchemaViolationRule

# Every implemented rule class, in run order. Each phase appends here.
_RULE_CLASSES: list[type[Rule]] = [SchemaViolationRule, ToolErrorEventRule, ErrorHandlingRule]


def rule_ids() -> list[str]:
    """The ids of every implemented rule (e.g. ``["R1"]``)."""
    return [cls.id for cls in _RULE_CLASSES]


def default_rules() -> list[Rule]:
    """The rules run by ``tracelint check`` when no subset is named. Grows each phase."""
    return [cls() for cls in _RULE_CLASSES]


def select_rules(names: list[str] | None = None) -> list[Rule]:
    """Instantiate the rules named in ``names`` (default: all), raising on an unknown id."""
    if not names:
        return default_rules()
    by_id = {cls.id: cls for cls in _RULE_CLASSES}
    selected: list[Rule] = []
    for name in names:
        if name not in by_id:
            raise ValueError(f"unknown rule {name!r}; known rules: {', '.join(by_id)}")
        selected.append(by_id[name]())
    return selected


__all__ = [
    "Rule",
    "lint_trace",
    "SchemaViolationRule",
    "ToolErrorEventRule",
    "ErrorHandlingRule",
    "default_rules",
    "select_rules",
    "rule_ids",
]
