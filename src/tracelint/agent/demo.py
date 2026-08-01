"""A small, keyless demo agent: the order-cancellation scenario (spec §II.1 user story).

Provides a toolset and canned tool behaviour so a scripted run produces a realistic trace with no
network or API key. Later phases reuse this toolset to plant one defect of each type; Phase 1 uses
it for a clean control run and, in tests, a single planted schema violation.
"""

from __future__ import annotations

from typing import Any

from tracelint.agent.react import ReActAgent
from tracelint.agent.scripted import ScriptedLLM, final, tool
from tracelint.agent.tools import AgentTool, AgentToolset, ToolError
from tracelint.tools import ToolMetadata
from tracelint.trace import Trace

# A tiny in-memory "backend" the demo tools read from.
_ORDERS: dict[str, dict[str, Any]] = {
    "4521": {"status": "processing", "shipped": False},
    "9001": {"status": "shipped", "shipped": True},
}

_CANCEL_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "pattern": "^[0-9]{3,10}$"},
        "reason": {
            "type": "string",
            "enum": ["customer_request", "duplicate", "fraud", "not_shipped"],
        },
    },
    "required": ["order_id", "reason"],
    "additionalProperties": False,
}

_STATUS_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string", "pattern": "^[0-9]{3,10}$"}},
    "required": ["order_id"],
    "additionalProperties": False,
}


def _get_order_status(args: dict[str, Any]) -> dict[str, Any]:
    order = _ORDERS.get(str(args.get("order_id")))
    if order is None:
        raise ToolError("order not found", http_status=404)
    return {"order_id": args["order_id"], "status": order["status"], "shipped": order["shipped"]}


def _cancel_order(args: dict[str, Any]) -> dict[str, Any]:
    order = _ORDERS.get(str(args.get("order_id")))
    if order is None:
        raise ToolError("order not found", http_status=404)
    if order["shipped"]:
        raise ToolError("cannot cancel a shipped order", http_status=409)
    return {"order_id": args["order_id"], "cancelled": True, "status": "cancellation_pending"}


def build_demo_toolset() -> AgentToolset:
    """The order-management toolset used across the demos and the validation suite."""
    return AgentToolset(
        [
            AgentTool(
                name="get_order_status",
                description="Look up an order's current status and whether it has shipped.",
                schema=_STATUS_SCHEMA,
                func=_get_order_status,
                metadata=ToolMetadata(idempotent=True),
            ),
            AgentTool(
                name="cancel_order",
                description="Cancel an order that has not yet shipped.",
                schema=_CANCEL_SCHEMA,
                func=_cancel_order,
                metadata=ToolMetadata(side_effecting=True),
            ),
        ]
    )


DEMO_TASK = "Cancel order 4521 if it hasn't shipped yet."


def _clean_script() -> list:
    return [
        tool("get_order_status", {"order_id": "4521"}, thought="First, check whether it shipped."),
        tool("cancel_order", {"order_id": "4521", "reason": "not_shipped"},
             thought="It hasn't shipped, so cancel it."),
        final("Order 4521 has not shipped, so I've cancelled it (cancellation pending)."),
    ]


def run_demo() -> tuple[Trace, AgentToolset]:
    """Run the clean scripted scenario, returning its trace and the toolset (for its registry)."""
    toolset = build_demo_toolset()
    agent = ReActAgent(
        ScriptedLLM(_clean_script()), toolset, system="You are an order-support agent."
    )
    trace = agent.run(DEMO_TASK, run_id="demo-clean")
    return trace, toolset
