"""tracelint — a deterministic, judge-free static analyzer for tool-calling agent traces.

The names re-exported here are the **supported public API** and follow semantic versioning.
Everything else remains importable from its submodule (e.g. ``tracelint.rules``) but is internal
and may change between minor versions.

The linter reads a trace and emits structured *findings*; it never calls a model to judge. See
``PROJECTS-TECHNICAL-SPEC.md`` Part II for the authoritative design.
"""

# ruff: noqa: I001 — imports grouped by role (matching __all__), not alphabetically.

from importlib.metadata import PackageNotFoundError, version as _pkg_version

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
    MalformedArgumentsRule,
    RedundantCallRule,
    Rule,
    SchemaViolationRule,
    ToolErrorEventRule,
    UnknownToolRule,
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
from tracelint.experiment import Condition, Experiment, render_experiment, run_experiment
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
from tracelint.adapters import (
    from_langfuse_trace,
    from_openai_messages,
    from_otel_spans,
    observed_tool_names,
    openai_tools_to_registry,
)

# --- Reporting ----------------------------------------------------------------------
from tracelint.report import (
    read_json,
    render_html,
    render_report,
    render_reports,
    write_html,
    write_json,
)

# --- Source on-ramps: load a provider format and lint it ----------------------------
from tracelint.sources import (
    SUPPORTED_FORMATS,
    lint_langfuse_trace,
    lint_openai_trace,
    lint_otel_trace,
    load_source,
)

__all__ = [
    # Trace schema
    "Trace",
    "Step",
    "Message",
    "ToolCall",
    "ToolResult",
    "StepMeta",
    "Role",
    "ResultStatus",
    "build_trace",
    "load_traces",
    # Tools
    "ToolRegistry",
    "ToolSpec",
    "ToolMetadata",
    # Findings
    "Finding",
    "ConfidenceTier",
    "LintReport",
    # Rules
    "Rule",
    "lint_trace",
    "SchemaViolationRule",
    "ToolErrorEventRule",
    "ErrorHandlingRule",
    "HallucinatedArgRule",
    "LoopRule",
    "RedundantCallRule",
    "MalformedArgumentsRule",
    "UnknownToolRule",
    "default_rules",
    # Provenance
    "build_provenance",
    "ProvenanceGraph",
    "SourceType",
    # Reliability
    "FaultInjector",
    "FaultType",
    "TargetedInjection",
    "RandomInjection",
    "apply_fault",
    "wilson_interval",
    "bootstrap_mean_ci",
    "aggregate_runs",
    "lint_runs",
    "ReproductionReport",
    "FindingReproduction",
    "run_experiment",
    "render_experiment",
    "Experiment",
    "Condition",
    # Scorecard
    "Task",
    "Scorecard",
    "FaultRecovery",
    "run_scorecard",
    "render_scorecard",
    "tool_called",
    "final_answer_contains",
    "final_answer_not_claims",
    "state_check",
    "all_of",
    # Adapters
    "from_openai_messages",
    "openai_tools_to_registry",
    "from_langfuse_trace",
    "observed_tool_names",
    "from_otel_spans",
    # Reporting
    "render_report",
    "render_reports",
    "render_html",
    "write_json",
    "write_html",
    "read_json",
    # Source on-ramps
    "load_source",
    "lint_otel_trace",
    "lint_openai_trace",
    "lint_langfuse_trace",
    "SUPPORTED_FORMATS",
]

# The installed distribution is the single source of truth (keeps this in lockstep with
# pyproject.toml). The fallback is only reached in a source checkout that isn't pip-installed.
try:
    __version__ = _pkg_version("tracelint")
except PackageNotFoundError:  # pragma: no cover - exercised only in an uninstalled checkout
    __version__ = "0.5.0"
