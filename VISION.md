# Vision & roadmap

tracelint reads what an agent **actually did** — the execution trace — and flags structural bugs
deterministically, with no LLM judge. This document is where the project is headed and, just as
importantly, where it is *deliberately not* headed.

## What tracelint is

**A developer tool: a deterministic linter for agent execution traces, run in the test suite and
CI.** It belongs where your other checks already live — pre-merge, offline, portable, no account.
The core loop is the whole product:

```
run agent in tests → capture the trace → run tracelint → fail CI on provable defects
```

Everything else is an optional layer around that core. Three properties define the engine underneath:

- **Portable** — it works across the trace formats your runs already produce: OpenTelemetry /
  OpenInference, OpenAI messages, native JSON, and platform exports (Langfuse, Arize Phoenix,
  LangSmith). One canonical trace model underneath, so the same checks apply no matter where the run
  came from. A neutral engine that spans all of them is something only an independent tool can be.
- **Deterministic** — same trace in, same findings out. Every finding points at exact evidence in the
  trace. Hard defects fail CI; heuristics stay advisory. No model is asked to judge.
- **Engine** — the rules you see today are the standard library, not the ceiling. The value is the
  execution-semantics layer beneath them: identifying calls, results, errors, provenance, side effects,
  repetition, and the causal relationships between them.

## The three-tier model

Where tracelint sits relative to the platforms people already run:

1. **Core — depends on no platform.** Frameworks and trace formats only. OpenTelemetry / OpenInference
   is a *format*, not a platform, so it stays the universal spine alongside native JSON and OpenAI
   messages. tracelint is stateless: it never stores traces — that is not its job.
2. **Read convenience — kept, lower priority.** Import a specific trace from a platform to lint or
   reproduce it (e.g. pull a failed production trace as a fixture). The zero-dependency fallback —
   export the trace to a file and `check` it — is always the documented baseline; per-platform pull
   is sugar on top, added as users ask.
3. **Write-back — an advanced recipe, frozen.** tracelint can write its verdict back into a platform's
   own score model, but that is the one layer that overlaps with what those platforms increasingly do
   themselves. It stays functional as an advanced recipe and is not part of the core narrative; no
   further write-back work is planned.

## What tracelint is *not*

tracelint is **not an observability platform** and will not try to become one. It builds no trace
viewer, no dashboard, no dataset store, no annotation UI, no prompt manager, no general evaluation
platform. Those systems already exist and are good at what they do. tracelint is a deterministic
check you run against their output — designed to be something those platforms are glad exists, not
something they need to reproduce.

## Roadmap

Near-term work is all in service of the identity above: make the engine excellent and the
capture→lint→CI loop frictionless, everywhere traces come from.

- **Frictionless capture.** The first run should not require you to already have a trace file — a
  one-call capture helper records a run to a lintable trace by wrapping the instrumentation your
  framework already ships.
- **Bulletproof first run.** Never crash on an unfamiliar trace shape; give useful output even before
  a tool contract exists.
- **Effortless onboarding.** `tracelint init` bootstraps a tool contract from a trace so you fill in
  only what the trace cannot know.
- **Real-trace coverage.** Golden fixtures and regression tests drawn from real runs across the major
  agent frameworks, so adopters' traces don't surprise it.
- **Read convenience.** Pull a trace in from a platform to lint or reproduce it — demand-gated per
  platform, always with the export-to-file baseline underneath.

### Explicitly deferred — gated on real user demand

The following are promising directions, but the project will **not** build them speculatively. Each
waits until real users ask for it, because each adds conceptual weight and the core loop has to
earn that first:

- **Developer-defined invariants** — application-specific contracts ("never issue a refund after a
  fraud block", "no account mutation after identity verification fails") evaluated deterministically
  over a whole trace. High value for the teams that need it; also higher onboarding cost, so it belongs
  to motivated users, not everyone.
- **Deterministic regression analysis** — compare two agent versions on structural behavior and
  contracts, not just final-answer quality.
- **Fault injection & failure-handling tests** — deliberately break a tool and verify the agent still
  behaves safely. Powerful, but it requires *running* the agent rather than only reading a trace, so it
  is a heavier, later step that sits outside the clean read-only design.

The guiding principle: **the vision is the destination; real usage is the compass.** tracelint stays a
focused, portable engine and expands only where users actively pull it.
