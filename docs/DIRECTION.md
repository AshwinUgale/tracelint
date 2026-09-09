# Direction notes

A short, dated log of deliberate direction changes — what changed and when, factually. For the full
positioning and roadmap, see [`VISION.md`](../VISION.md).

## 2026-09-09 — dev-tool / CI-first reposition

tracelint is positioned as a **developer tool** whose core loop is the product:

```
run agent in tests → capture the trace → run tracelint → fail CI on provable defects
```

What changed:

- **Capture on-ramp added.** `tracelint.capture` records an agent run to a lintable trace by
  wrapping the framework's stock OpenInference instrumentation, so the first run no longer assumes
  you already have a trace file. Covers the in-process frameworks (smolagents, LangGraph / LangChain,
  CrewAI); Langflow uses an OTel-to-file recipe.
- **Three-tier platform model made explicit.** (1) Core depends on no platform — frameworks and
  trace formats only, with OTel/OpenInference as a format, not a platform. (2) Reading a trace from a
  platform is a kept, lower-priority convenience on top of the export-to-file baseline. (3) Writing
  findings back into a platform is demoted to an advanced recipe and frozen — it overlaps with what
  platforms increasingly do themselves.
- **README + VISION reordered** to lead with the capture→lint→CI path and to present write-back only
  as an advanced recipe.

Write-back remains functional; it is repositioned, not removed.
