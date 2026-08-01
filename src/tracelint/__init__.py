"""tracelint — a deterministic, judge-free static analyzer for tool-calling agent traces.

The names re-exported here are the **supported public API** and follow semantic versioning.
Everything else remains importable from its submodule (e.g. ``tracelint.rules``) but is internal
and may change between minor versions.

The linter reads a trace and emits structured *findings*; it never calls a model to judge. See
``PROJECTS-TECHNICAL-SPEC.md`` Part II for the authoritative design.
"""

# ruff: noqa: I001 — imports grouped by role (matching __all__), not alphabetically.

# --- Canonical trace schema ---------------------------------------------------------
from tracelint.trace import (
    Message,
    ResultStatus,
    Role,
    Step,
    StepMeta,
    ToolCall,
    ToolResult,
    Trace,
    build_trace,
    load_traces,
)

# --- Tool ground truth --------------------------------------------------------------
from tracelint.tools import ToolMetadata, ToolRegistry, ToolSpec

# --- Findings + report --------------------------------------------------------------
from tracelint.findings import ConfidenceTier, Finding, LintReport

# --- Rules + driver -----------------------------------------------------------------
from tracelint.rules import Rule, SchemaViolationRule, default_rules, lint_trace

# --- Adapters -----------------------------------------------------------------------
from tracelint.adapters import from_openai_messages, openai_tools_to_registry

__all__ = [
    # Trace schema
    "Trace", "Step", "Message", "ToolCall", "ToolResult", "StepMeta",
    "Role", "ResultStatus", "build_trace", "load_traces",
    # Tools
    "ToolRegistry", "ToolSpec", "ToolMetadata",
    # Findings
    "Finding", "ConfidenceTier", "LintReport",
    # Rules
    "Rule", "lint_trace", "SchemaViolationRule", "default_rules",
    # Adapters
    "from_openai_messages", "openai_tools_to_registry",
]

__version__ = "0.1.0"
