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
from tracelint.rules import (
    ErrorHandlingRule,
    HallucinatedArgRule,
    LoopRule,
    RedundantCallRule,
    Rule,
    SchemaViolationRule,
    ToolErrorEventRule,
    default_rules,
    lint_trace,
)

# --- Provenance ---------------------------------------------------------------------
from tracelint.provenance import ProvenanceGraph, SourceType, build_provenance

# --- Reliability: fault injection, statistics, nondeterminism -----------------------
from tracelint.injection import (
    FaultInjector,
    FaultType,
    RandomInjection,
    TargetedInjection,
    apply_fault,
)
from tracelint.stats import bootstrap_mean_ci, wilson_interval
from tracelint.nondeterminism import (
    FindingReproduction,
    ReproductionReport,
    aggregate_runs,
    lint_runs,
)

# --- Recovery scorecard -------------------------------------------------------------
from tracelint.scorecard import (
    FaultRecovery,
    Scorecard,
    Task,
    all_of,
    final_answer_contains,
    final_answer_not_claims,
    render_scorecard,
    run_scorecard,
    state_check,
    tool_called,
)

# --- Adapters -----------------------------------------------------------------------
from tracelint.adapters import from_openai_messages, openai_tools_to_registry

# --- Reporting ----------------------------------------------------------------------
from tracelint.report import (
    read_json,
    render_html,
    render_report,
    render_reports,
    write_html,
    write_json,
)

__all__ = [
    # Trace schema
    "Trace", "Step", "Message", "ToolCall", "ToolResult", "StepMeta",
    "Role", "ResultStatus", "build_trace", "load_traces",
    # Tools
    "ToolRegistry", "ToolSpec", "ToolMetadata",
    # Findings
    "Finding", "ConfidenceTier", "LintReport",
    # Rules
    "Rule", "lint_trace", "SchemaViolationRule", "ToolErrorEventRule", "ErrorHandlingRule",
    "HallucinatedArgRule", "LoopRule", "RedundantCallRule", "default_rules",
    # Provenance
    "build_provenance", "ProvenanceGraph", "SourceType",
    # Reliability
    "FaultInjector", "FaultType", "TargetedInjection", "RandomInjection", "apply_fault",
    "wilson_interval", "bootstrap_mean_ci",
    "aggregate_runs", "lint_runs", "ReproductionReport", "FindingReproduction",
    # Scorecard
    "Task", "Scorecard", "FaultRecovery", "run_scorecard", "render_scorecard",
    "tool_called", "final_answer_contains", "final_answer_not_claims", "state_check", "all_of",
    # Adapters
    "from_openai_messages", "openai_tools_to_registry",
    # Reporting
    "render_report", "render_reports", "render_html", "write_json", "write_html", "read_json",
]

__version__ = "0.1.0"
