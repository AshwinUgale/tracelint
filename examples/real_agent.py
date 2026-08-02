"""Run tracelint on a REAL OpenAI agent (not the scripted demo).

A genuine GPT model drives a small customer-support toolset, some of whose tools can error. The
model's real decisions produce a real trace, which tracelint then lints. This is the "point it at
a live agent" path — the model, not a script, decides what to do, so the defects tracelint reports
are ones an actual model made.

Usage (needs your own OpenAI key — you run this, tracelint never sees the key):

    pip install "tracelint[real-agent]"
    export OPENAI_API_KEY=sk-...            # (Windows: set OPENAI_API_KEY=sk-...)
    python examples/real_agent.py --task "Refund my order A100 for the full amount."
    python examples/real_agent.py --task "Refund order Z999." --model gpt-4o-mini --html run.html

The toolset (`build_support_toolset`) and runner (`run_with_llm`) are importable and model-agnostic,
so the same scenario is exercised deterministically in the test suite with a stub client.
"""

from __future__ import annotations

import argparse
import sys

from tracelint.agent.react import ReActAgent
from tracelint.agent.tools import AgentTool, AgentToolset, ToolError
from tracelint.report import render_html, render_report, write_html
from tracelint.rules import default_rules, lint_trace
from tracelint.tools import ToolMetadata

# A tiny in-memory backend. The model does not see this — it only sees the tools' results.
_ORDERS = {
    "A100": {"status": "delivered", "amount": 49.99, "refundable": True},
    "A200": {"status": "shipped", "amount": 129.00, "refundable": False},
}

_LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}
_ELIGIBILITY_SCHEMA = _LOOKUP_SCHEMA
_REFUND_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string"},
        # Annotated 'provided': the amount must come from the lookup, so an invented amount is a
        # high-confidence (hard_defect) hallucination rather than a candidate.
        "amount": {"type": "number", "x-value-origin": "provided"},
    },
    "required": ["order_id", "amount"],
    "additionalProperties": False,
}


def _lookup_order(args: dict) -> dict:
    order = _ORDERS.get(str(args.get("order_id")))
    if order is None:
        raise ToolError("order not found", http_status=404)
    return {"order_id": args["order_id"], **order}


def _check_refund_eligibility(args: dict) -> dict:
    order = _ORDERS.get(str(args.get("order_id")))
    if order is None:
        raise ToolError("order not found", http_status=404)
    return {"order_id": args["order_id"], "eligible": bool(order["refundable"])}


def _issue_refund(args: dict) -> dict:
    order = _ORDERS.get(str(args.get("order_id")))
    if order is None:
        raise ToolError("order not found", http_status=404)
    if not order["refundable"]:
        raise ToolError("order is not refundable", http_status=409)
    return {"order_id": args["order_id"], "refunded": True, "amount": args.get("amount")}


def build_support_toolset() -> AgentToolset:
    """A refund-support toolset: look up an order, check eligibility, issue a refund."""
    return AgentToolset(
        [
            AgentTool(
                "lookup_order", "Look up an order's status, amount, and refund eligibility.",
                _LOOKUP_SCHEMA, _lookup_order, ToolMetadata(idempotent=True),
            ),
            AgentTool(
                "check_refund_eligibility", "Check whether an order can be refunded.",
                _ELIGIBILITY_SCHEMA, _check_refund_eligibility, ToolMetadata(idempotent=True),
            ),
            AgentTool(
                "issue_refund", "Issue a refund for an order (only if eligible).",
                _REFUND_SCHEMA, _issue_refund, ToolMetadata(side_effecting=True),
            ),
        ]
    )


SYSTEM = (
    "You are a careful customer-support agent. Look up the order first, verify it is refundable, "
    "and only then issue a refund for the exact amount from the lookup. If a tool errors, do not "
    "proceed as if it succeeded."
)


def run_with_llm(llm, task_text: str, *, run_id: str = "real-run", max_steps: int = 8):
    """Run the support agent with any LLM backend; returns (trace, toolset)."""
    toolset = build_support_toolset()
    agent = ReActAgent(llm, toolset, system=SYSTEM, max_steps=max_steps)
    return agent.run(task_text, run_id=run_id), toolset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint a real OpenAI agent run.")
    parser.add_argument("--task", default="Refund my order A100 for the full amount.")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--html", dest="html_out", help="write an HTML report")
    args = parser.parse_args(argv)

    try:
        from tracelint.agent.openai_llm import OpenAILLM
    except ImportError:
        print("Install the extra first: pip install 'tracelint[real-agent]'", file=sys.stderr)
        return 3

    llm = OpenAILLM(model=args.model)  # reads OPENAI_API_KEY from the environment
    trace, toolset = run_with_llm(llm, args.task)

    print(f"\n--- trace ({len(trace.tool_calls())} tool calls) ---")
    print(trace.to_json())

    report = lint_trace(trace, default_rules(), toolset.to_registry())
    print("\n--- tracelint ---")
    print(render_report(report, include_candidates=True))

    if args.html_out:
        write_html(args.html_out, render_html(title="tracelint — real agent run", reports=[report]))
        print(f"\nwrote {args.html_out}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
