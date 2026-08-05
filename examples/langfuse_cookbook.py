"""Cookbook — lint your Langfuse agent traces with tracelint (judge-free, CI-gated).

tracelint reads a trace and reports *structural* defects (schema violations, ignored tool errors,
hallucinated arguments, loops) with the exact evidence and a CI exit code — no second model judges
the trace. This example runs it on the traces you already collect in Langfuse.

Two modes:

- **default (offline, keyless)** — lints a bundled Langfuse-shaped sample trace so you can see the
  whole flow with no account and no key.
- **``--trace-id <id>`` (live)** — fetches a real trace via the Langfuse SDK, lints it, and can
  write the findings back as Langfuse **scores** so they show up next to the trace in the UI.

Live mode needs::

    pip install "tracelint[langfuse]"
    export LANGFUSE_PUBLIC_KEY=pk-...    # and LANGFUSE_SECRET_KEY, LANGFUSE_HOST if self-hosted
    python examples/langfuse_cookbook.py --trace-id <id> --tools-file tools.json --push-scores

tracelint never calls a model — it only reads the trace you already captured.
"""

from __future__ import annotations

import argparse

from tracelint import (
    ConfidenceTier,
    ToolRegistry,
    default_rules,
    from_langfuse_trace,
    lint_trace,
    render_report,
)
from tracelint.findings import LintReport

# --- A bundled, Langfuse-shaped sample trace (offline demo) ---------------------------
#
# The agent looked up a non-existent order (a 404 error it then ignored) and tried to issue a
# refund with a malformed ``amount`` (a string where the schema requires a number). tracelint
# flags the schema violation as a hard defect (exit 2) and the tool error as an event.

SAMPLE_TRACE = {
    "id": "demo-trace-1",
    "input": "Refund order Z999 for the full amount.",
    "output": "Refunded order Z999.",
    "observations": [
        {
            "id": "gen-1",
            "type": "generation",
            "startTime": "2024-01-01T00:00:00Z",
            "output": {"content": "Looking up order Z999 so I can refund it."},
        },
        {
            "id": "obs-lookup",
            "type": "tool",
            "name": "lookup_order",
            "startTime": "2024-01-01T00:00:01Z",
            "input": {"order_id": "Z999"},
            "output": {"http_status": 404, "detail": "order not found"},
            "level": "ERROR",
            "statusMessage": "order not found",
        },
        {
            "id": "obs-refund",
            "type": "tool",
            "name": "issue_refund",
            "startTime": "2024-01-01T00:00:02Z",
            "input": {"order_id": "Z999", "amount": "fifty"},  # schema wants a number
            "output": {"refunded": True},
        },
    ],
}

SAMPLE_TOOLS = {
    "tools": {
        "lookup_order": {
            "schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            }
        },
        "issue_refund": {
            "schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"}},
                "required": ["order_id", "amount"],
            },
            "metadata": {"side_effecting": True},
        },
    }
}

SAMPLE_TOOL_NAMES = ["lookup_order", "issue_refund"]


def lint_langfuse(
    trace: object,
    *,
    registry: ToolRegistry | None,
    tool_names: list[str] | None,
) -> LintReport:
    """Normalize a Langfuse trace and lint it with the default rule set."""
    canonical = from_langfuse_trace(trace, tool_names=tool_names)
    return lint_trace(canonical, default_rules(), registry)


def _fetch_trace(trace_id: str) -> object:
    """Fetch a full trace (with observations) via the Langfuse SDK. Live mode only."""
    from langfuse import Langfuse  # optional dependency: pip install "tracelint[langfuse]"

    client = Langfuse()
    # SDK versions differ; try the common access paths and return a plain dict.
    getter = getattr(getattr(client, "api", client), "trace", None)
    if getter is not None and hasattr(getter, "get"):
        return getter.get(trace_id)
    return client.get_trace(trace_id)  # older SDKs


def _create_score(client: object, trace_id: str, name: str, value: float, comment: str) -> None:
    """Write one score, tolerating SDK naming differences (create_score vs score)."""
    for method in ("create_score", "score"):
        fn = getattr(client, method, None)
        if callable(fn):
            fn(trace_id=trace_id, name=name, value=value, comment=comment)
            return
    raise RuntimeError("Langfuse client exposes neither create_score nor score")


def push_findings_as_scores(trace_id: str, report: LintReport) -> None:
    """Write tracelint's verdict back to the Langfuse trace as scores (live mode)."""
    from langfuse import Langfuse

    client = Langfuse()
    n_defects = len(report.by_tier(ConfidenceTier.HARD_DEFECT))
    comment = "; ".join(f.summary for f in report.active_findings) or "no findings"
    _create_score(client, trace_id, "tracelint_hard_defects", float(n_defects), comment)
    _create_score(
        client,
        trace_id,
        "tracelint_status",
        0.0 if report.has_hard_defect else 1.0,
        "fail (hard defect)" if report.has_hard_defect else "pass",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint Langfuse agent traces with tracelint.")
    parser.add_argument("--trace-id", help="fetch this Langfuse trace and lint it (live mode)")
    parser.add_argument("--tools-file", help="tools.json (ToolRegistry) for R1/R3 in live mode")
    parser.add_argument(
        "--tool-names",
        nargs="*",
        help="observation names to treat as tool calls (span-based instrumentation)",
    )
    parser.add_argument(
        "--push-scores",
        action="store_true",
        help="write findings back to the trace as Langfuse scores",
    )
    args = parser.parse_args(argv)

    if args.trace_id:
        trace = _fetch_trace(args.trace_id)
        registry = ToolRegistry.load(args.tools_file) if args.tools_file else None
        report = lint_langfuse(trace, registry=registry, tool_names=args.tool_names)
    else:
        print("No --trace-id given: linting the bundled offline sample trace.\n")
        registry = ToolRegistry.from_dict(SAMPLE_TOOLS)
        report = lint_langfuse(SAMPLE_TRACE, registry=registry, tool_names=SAMPLE_TOOL_NAMES)

    print(render_report(report, include_candidates=True))

    if args.trace_id and args.push_scores:
        push_findings_as_scores(args.trace_id, report)
        print(f"\nWrote tracelint findings back to Langfuse trace {args.trace_id} as scores.")

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
