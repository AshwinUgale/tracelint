"""Generate a REAL Langfuse trace from tracelint's own GPT agent, then fetch + lint it.

This is the strongest validation of the Langfuse adapter: a real model drives the refund agent,
the Langfuse SDK captures the run (LLM generations via the OpenAI drop-in, tool executions as
observations) exactly as it would for any app, and tracelint reads that real trace back. It also
**dumps the raw fetched trace** to a file, so the adapter can be checked against the actual bytes
Langfuse returns — not a hand-built fixture.

You run this (these scripts never see your keys)::

    pip install "tracelint[real-agent,langfuse]"
    export OPENAI_API_KEY=sk-...
    export LANGFUSE_PUBLIC_KEY=pk-...   # and LANGFUSE_SECRET_KEY (LANGFUSE_HOST if self-hosted)
    python examples/langfuse_generate_and_lint.py --task "Refund order Z999." --dump real_trace.json

Targets the Langfuse Python SDK v4 (uses ``start_observation``). If a Langfuse emit call fails on
your SDK version, share the error and ``pip show langfuse`` — but note the *fetched-trace dump* is
the artifact that matters for tightening the adapter, and it is produced regardless of emit path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from tracelint import (
    ConfidenceTier,
    ToolRegistry,
    default_rules,
    from_langfuse_trace,
    lint_trace,
    render_report,
)

# --- Lint + score write-back (self-contained so the script runs standalone) ------------


def lint_langfuse(trace: object, *, registry: ToolRegistry | None, tool_names: list[str] | None):
    """Normalize a Langfuse trace and lint it with the default rule set."""
    canonical = from_langfuse_trace(trace, tool_names=tool_names)
    return lint_trace(canonical, default_rules(), registry)


def _create_score(client: object, trace_id: str, name: str, value: float, comment: str) -> None:
    for method in ("create_score", "score"):
        fn = getattr(client, method, None)
        if callable(fn):
            fn(trace_id=trace_id, name=name, value=value, comment=comment)
            return
    raise RuntimeError("Langfuse client exposes neither create_score nor score")


def push_findings_as_scores(trace_id: str, report: object) -> None:
    """Write tracelint's verdict back to the Langfuse trace as scores (best-effort, live mode)."""
    try:
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
        print("Wrote tracelint scores back to the Langfuse trace.")
    except Exception as exc:  # noqa: BLE001 - scoring is optional; never fail the run over it
        print(f"(Could not write scores back to Langfuse on this SDK version: {exc})")


# --- Langfuse instrumentation of the agent's tools (v4 @observe decorator) --------------


def _instrument_toolset(toolset: object, observe) -> None:
    """Wrap each tool with Langfuse's ``@observe`` so its execution nests as an observation.

    v4's decorator manages the OTel context, so the tool observations (and the LLM generations
    from the OpenAI drop-in) nest under the root and share one trace — which manual
    ``start_observation`` calls did not do. ``observe`` is injected (the real ``langfuse.observe``
    live; a fake in tests) so the wiring stays testable offline. On a tool error the decorator
    records the exception and marks the observation ``level=ERROR`` — the structured signal R2a
    reads; the tool name is passed so the adapter recognizes the span via ``tool_names``.
    """
    for tool in toolset._tools.values():  # internal access is fine for an example
        try:
            decorator = observe(name=tool.name, as_type="tool")
        except TypeError:
            decorator = observe(name=tool.name)
        tool.func = decorator(tool.func)


def _run_agent_with_langfuse(task: str, model: str) -> tuple[object, str, object]:
    """Run the refund agent under Langfuse tracing; return (client, trace_id, toolset).

    Uses the v4 ``@observe`` decorator so the root, the LLM generations (via the OpenAI drop-in),
    and the tool observations all nest into one trace. ``get_current_trace_id`` is read *inside*
    the decorated function, where the trace context is active.
    """
    from langfuse import Langfuse, observe
    from langfuse.openai import OpenAI as LangfuseOpenAI

    # Allow running as a script (`python examples/...`), when the repo root isn't on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from examples.real_agent import SYSTEM, build_support_toolset
    from tracelint.agent.openai_llm import OpenAILLM
    from tracelint.agent.react import ReActAgent

    langfuse = Langfuse()
    captured: dict = {}

    @observe(name="refund-agent")
    def _run(user_task: str) -> str:
        toolset = build_support_toolset()
        _instrument_toolset(toolset, observe)
        agent = ReActAgent(
            OpenAILLM(model=model, client=LangfuseOpenAI()), toolset, system=SYSTEM, max_steps=8
        )
        trace = agent.run(user_task, run_id="live-run")
        captured["toolset"] = toolset
        captured["trace_id"] = langfuse.get_current_trace_id()
        return trace.final

    _run(task)
    langfuse.flush()
    return langfuse, captured.get("trace_id"), captured["toolset"]


def _fetch_once(langfuse: object, trace_id: str) -> object:
    getter = getattr(getattr(langfuse, "api", langfuse), "trace", None)
    if getter is not None and hasattr(getter, "get"):
        return getter.get(trace_id)
    return langfuse.get_trace(trace_id)


def _fetch_with_retry(
    langfuse: object, trace_id: str, *, attempts: int = 10, delay: float = 3.0
) -> object:
    """Fetch the trace, retrying until its observations land.

    Langfuse ingestion is async and *child* observations often arrive after the trace shell, so a
    successful fetch that returns zero observations is retried rather than accepted — otherwise we
    would lint an empty trace. The shell is returned as a last resort so the caller still gets the
    dump for diagnosis.
    """
    last_exc: Exception | None = None
    shell: object | None = None
    for _ in range(attempts):
        try:
            trace = _fetch_once(langfuse, trace_id)
            if _to_plain_dict(trace).get("observations"):
                return trace
            shell = trace  # ingested but children not in yet — keep waiting
        except Exception as exc:  # noqa: BLE001 - transient until ingestion lands
            last_exc = exc
        time.sleep(delay)
    if shell is not None:
        return shell
    raise RuntimeError(f"trace {trace_id} not available after {attempts} attempts") from last_exc


def _to_plain_dict(trace: object) -> dict:
    if isinstance(trace, dict):
        return trace
    for attr in ("model_dump", "dict"):
        fn = getattr(trace, attr, None)
        if callable(fn):
            result = fn()
            if isinstance(result, dict):
                return result
    return json.loads(json.dumps(trace, default=lambda o: getattr(o, "__dict__", str(o))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a real Langfuse trace, then lint it.")
    parser.add_argument("--task", default="Refund order Z999 for the full amount.")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--dump", help="write the raw fetched Langfuse trace JSON here")
    parser.add_argument("--push-scores", action="store_true", help="write findings back as scores")
    args = parser.parse_args(argv)

    if not os.getenv("OPENAI_API_KEY") or not os.getenv("LANGFUSE_PUBLIC_KEY"):
        print("Set OPENAI_API_KEY and LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY first.")
        return 3

    langfuse, trace_id, toolset = _run_agent_with_langfuse(args.task, args.model)
    print(f"Ran the agent. Langfuse trace_id = {trace_id}\nFetching (ingestion is async)...")
    trace = _fetch_with_retry(langfuse, trace_id)
    raw = _to_plain_dict(trace)

    if args.dump:
        Path(args.dump).write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
        print(f"Raw fetched trace written to {args.dump} — send this to validate the adapter.\n")

    registry = ToolRegistry.from_dict(
        {
            "tools": {
                name: {"schema": s.schema, "metadata": vars(s.metadata)}
                for name, s in ((n, toolset.to_registry().get(n)) for n in toolset.names())
            }
        }
    )
    report = lint_langfuse(raw, registry=registry, tool_names=toolset.names())
    print(render_report(report, include_candidates=True))

    if args.push_scores:
        push_findings_as_scores(trace_id, report)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
