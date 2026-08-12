# Examples

## `lint_openinference_phoenix.py` — lint OpenInference / OpenTelemetry spans

Fully **offline and keyless**. Builds a small Arize Phoenix-shaped OpenInference span export for a
flight-booking agent that makes two real mistakes (a schema-violating tool call and a declined
payment), then lints it via `lint_otel_trace` — first schema-free, then with a `tools.json`-style
registry so R1 proves the schema violation a hard defect and the process exits `2`.

```bash
python examples/lint_openinference_phoenix.py

# The same thing at the command line, against any OpenInference export:
tracelint check spans.json --format openinference --tools tools.json
```

Because it reads OpenInference (the OpenTelemetry semantic convention for AI spans), the same path
reaches Arize Phoenix, OpenLLMetry, Langfuse-via-OTel, and datasets like TRAIL — one adapter, the
whole ecosystem. It is exercised by `tests/test_openinference_example.py`.

## `real_agent.py` — lint a real OpenAI agent run

A genuine GPT model drives a small refund-support toolset (some tools can error), and tracelint
lints the trace the model actually produced. Unlike `tracelint demo` (which is fully scripted and
keyless), this runs a live model, so the defects it finds are ones a real model made.

```bash
pip install "tracelint[real-agent]"
export OPENAI_API_KEY=sk-...          # Windows: set OPENAI_API_KEY=sk-...

# A clean run:
python examples/real_agent.py --task "Refund my order A100 for the full amount."

# A run more likely to trip a rule (the order does not exist):
python examples/real_agent.py --task "Refund order Z999." --model gpt-4o-mini --html run.html
```

The process exits `2` if the run contains a hard defect (e.g. the model issues a refund for an
invented amount — the `amount` field is schema-annotated `x-value-origin: "provided"`, so an
un-derivable value is a high-confidence hallucination), which makes this usable as a CI gate.

The toolset and runner are importable and model-agnostic, so `tests/test_real_agent_example.py`
exercises the exact same path deterministically with a stub client — no API key required.

## `langfuse_cookbook.py` — lint the traces you already collect in Langfuse

If you use [Langfuse](https://langfuse.com) for observability, tracelint runs *on top of* it:
it reads the traces Langfuse already captured and reports structural defects with a CI exit
code — judge-free, complementing Langfuse's LLM-as-judge evals. The `from_langfuse_trace`
adapter normalizes a Langfuse trace (native `tool` observations, span-based tools, or OpenAI-style
`tool_calls` in generations) into tracelint's schema.

```bash
# Offline, keyless — lints a bundled Langfuse-shaped sample trace so you can see the flow:
python examples/langfuse_cookbook.py

# Live — fetch a real trace, lint it, and write findings back as Langfuse scores:
pip install "tracelint[langfuse]"
export LANGFUSE_PUBLIC_KEY=pk-...   # and LANGFUSE_SECRET_KEY (LANGFUSE_HOST if self-hosted)
python examples/langfuse_cookbook.py --trace-id <id> --tools-file tools.json --push-scores
```

Bring your tool JSON schemas as a `tools.json` (`ToolRegistry`) — that is what R1 validates
against and what upgrades R3 to a hard defect; Langfuse traces rarely carry schemas themselves.
The offline path is exercised by `tests/test_langfuse_cookbook_example.py` with no key.

## `langfuse_generate_and_lint.py` — validate the adapter on a *real* Langfuse trace

The strongest check: a real GPT model drives the refund agent, the Langfuse SDK captures the run
(generations via the OpenAI drop-in, tool executions as observations) exactly as it would for any
app, and tracelint reads that real trace back — no hand-built fixture. It also **dumps the raw
fetched trace** so the adapter can be validated against the actual bytes Langfuse returns.

```bash
pip install "tracelint[real-agent,langfuse]"
export OPENAI_API_KEY=sk-...
export LANGFUSE_PUBLIC_KEY=pk-...   # and LANGFUSE_SECRET_KEY (LANGFUSE_HOST if self-hosted)
python examples/langfuse_generate_and_lint.py --task "Refund order Z999." --dump real_trace.json
```

Cost is negligible (free Langfuse tier + a few cents of gpt-4o-mini). The instrumentation logic is
tested offline with a fake client in `tests/test_langfuse_generate_example.py`; it targets the
Langfuse Python SDK v3+.
