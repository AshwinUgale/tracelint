# Examples

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
