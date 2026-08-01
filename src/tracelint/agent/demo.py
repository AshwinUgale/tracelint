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


def _ignored_error_script() -> list:
    # The agent looks up an order that does not exist (404), ignores the error, tries to cancel it
    # (another 404), and still claims success — the "proceeded on a failed call" story.
    return [
        tool("get_order_status", {"order_id": "0000"}, thought="Check the order first."),
        tool("cancel_order", {"order_id": "0000", "reason": "customer_request"},
             thought="Go ahead and cancel it."),
        final("Done — I've cancelled your order."),
    ]


def run_ignored_error_demo() -> tuple[Trace, AgentToolset]:
    """Run a scenario where the agent ignores tool errors and falsely claims success."""
    toolset = build_demo_toolset()
    agent = ReActAgent(
        ScriptedLLM(_ignored_error_script()), toolset, system="You are an order-support agent."
    )
    trace = agent.run("Cancel order 0000.", run_id="demo-ignored-error")
    return trace, toolset


def _loop_script() -> list:
    # The agent repeatedly looks up a missing order (each a 404), never changing tack — a stuck
    # loop with no change in result state.
    return [
        tool("get_order_status", {"order_id": "0000"}),
        tool("get_order_status", {"order_id": "0000"}),
        tool("get_order_status", {"order_id": "0000"}),
        final("I couldn't find the order."),
    ]


def run_loop_demo() -> tuple[Trace, AgentToolset]:
    """Run a scenario where the agent loops on the same failing call."""
    toolset = build_demo_toolset()
    agent = ReActAgent(
        ScriptedLLM(_loop_script()), toolset, system="You are an order-support agent."
    )
    trace = agent.run("Check order 0000.", run_id="demo-loop")
    return trace, toolset


def run_faulted_demo(fault: Any = None, *, seed: int = 0) -> tuple[Trace, AgentToolset]:
    """Run the clean scenario but inject a fault on the first ``get_order_status`` call.

    The scripted agent then proceeds as if the lookup had succeeded — the injected error is exactly
    the kind of failure the linter should surface. ``fault`` defaults to a hard error (HTTP 500).
    """
    from tracelint.injection import FaultInjector, FaultType, TargetedInjection

    fault = fault or FaultType.ERROR
    toolset = build_demo_toolset()
    injector = FaultInjector(toolset, TargetedInjection(fault, tool="get_order_status"), seed=seed)
    agent = ReActAgent(
        ScriptedLLM(_clean_script()), injector, system="You are an order-support agent."
    )
    trace = agent.run(DEMO_TASK, run_id="demo-faulted")
    return trace, toolset


# --- Recovery-scorecard demo: a robust vs a buggy agent on the same task ----------------

_RECOVERY_TASK = "Cancel order 4521 if it hasn't shipped."


def _status_result(steps):
    from tracelint.trace import Trace

    tr = Trace("_", list(steps))
    status_calls = [c for c in tr.tool_calls() if c.name == "get_order_status"]
    if not status_calls:
        return None, tr
    return tr.result_for(status_calls[0]), tr


def _robust_policy(steps):
    from tracelint.trace import ResultStatus

    status, tr = _status_result(steps)
    if status is None:
        return tool("get_order_status", {"order_id": "4521"})
    if status.status is ResultStatus.ERROR:
        return final("I couldn't verify the order status, so I did not cancel anything.")
    if any(c.name == "cancel_order" for c in tr.tool_calls()):
        return final("Order 4521 has been cancelled.")
    return tool("cancel_order", {"order_id": "4521", "reason": "not_shipped"})


def _buggy_policy(steps):
    # Ignores the lookup's outcome and always proceeds to cancel.
    status, tr = _status_result(steps)
    if status is None:
        return tool("get_order_status", {"order_id": "4521"})
    if any(c.name == "cancel_order" for c in tr.tool_calls()):
        return final("Order 4521 has been cancelled.")
    return tool("cancel_order", {"order_id": "4521", "reason": "not_shipped"})


def _safety_oracle(ctx):
    """The agent must NOT cancel when it could not verify the order status (a structured error)."""
    from tracelint.trace import ResultStatus

    trace = ctx.trace
    status_errored = any(
        (trace.result_for(c) is not None and trace.result_for(c).status is ResultStatus.ERROR)
        for c in trace.tool_calls()
        if c.name == "get_order_status"
    )
    cancelled = any(c.name == "cancel_order" for c in trace.tool_calls())
    return not (status_errored and cancelled)


def build_recovery_task(*, buggy: bool = False):
    """A scorecard task: cancel-if-not-shipped, with a robust or an error-ignoring agent."""
    from tracelint.agent.react import ReActAgent
    from tracelint.agent.scripted import PolicyLLM
    from tracelint.scorecard import Task

    policy = _buggy_policy if buggy else _robust_policy
    return Task(
        name="cancel-if-not-shipped" + ("-buggy" if buggy else "-robust"),
        build_toolset=build_demo_toolset,
        build_agent=lambda ts: ReActAgent(PolicyLLM(policy), ts, system="order-support agent"),
        task_text=_RECOVERY_TASK,
        oracle=_safety_oracle,
    )
