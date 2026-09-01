# Roadmap

Deferred and exploratory work, captured so it isn't lost. **Nothing here is scheduled.**

The order is set by what real usage actually hits — not by how well an idea is argued in a comment
thread. Several items came from sharp reviewers; the honest trigger to build one is a real user
encountering it on a real trace, not the suggestion itself.

## Shipped since this file was first drafted

- **Fault-injection experiment** — `experiment.py` + `examples/fault_experiment.py` run a *live*
  agent under injected faults and report recovery / incorrect-continuation / tracelint-flagged rates,
  each with a Wilson interval. (Lesson baked in: at temperature 0 the model is deterministic, so the
  runs are identical and the interval is a lie — a rate needs independent samples, so the example
  defaults the agent to a sampling temperature.)
- **Adapter breadth** — the event-list reader now covers OpenInference *and* the OTel **GenAI**
  semconv (OpenLLMetry / Traceloop); the message-list reader now reads ShareGPT (`from`/`value`),
  `role`+`text`, and typed-block content.
- **R8 — duplicate side effect** — a non-idempotent side-effecting tool called again with equivalent
  arguments when the first call did **not** fail (the double-charge). `hard_event` when the first
  succeeded, `candidate` when its outcome is unknown; a repeat after a genuine failure is a
  legitimate retry and is never flagged. Uses only the existing `side_effecting` / `idempotent`
  metadata. This is the *first* half of the side-effect story; unconfirmed side effect (below) is the
  second, deferred until real usage justifies its new `confirmed_by` key.

## Candidate rules

### Unconfirmed side effect (the second half of the side-effect story; takes the next free rule id)

Today R2 catches *known* failures: a tool returned an error and the agent proceeded (R2a/R2b), or an
errored value was reused in a later side-effecting call. It does **not** catch the case where the
agent *never found out* whether a side-effecting action succeeded. Two shapes:

1. **No terminal status.** A side-effecting call whose span just stops — no result, no error, no
   `http_status`. Structurally it looks like an incomplete run rather than a defect, and the effect
   may or may not have happened. (Raised in review: *"not 'the agent ignored an error' but 'the
   agent never found out' — in our world that's the expensive one."*)
2. **No independent confirmation.** A side-effecting call that returned, but whose effect was never
   confirmed through a path other than the one that wrote it. The write API echoing its own result
   back is the same channel grading itself; a later `get_<resource>(id)` or a balance read brings a
   second source in. (Raised in review as the "read-back" idea.)

Design constraints, so it stays in character with the rest of the tool:

- **Candidate tier, fail-closed.** A call with no result at the *very end* of a trace is ambiguous —
  truncated capture vs. a real hang — so it must disclose a candidate, never assert a defect.
- **Declared, not guessed.** What counts as a confirming read is per-tool ground truth declared in
  `tools.json` (same model as `side_effecting` / `failure_when` / `x-value-origin`), e.g.
  `{"confirmed_by": {"tool": "get_transfer", "key": "/transfer_id"}}`. A side-effecting tool that
  declares none yields a per-call suppression, not a silent pass.
- **Score presence, not verdict.** "Side effects with no independent observation" is a countable
  property of a trace you can watch across releases; a static linter cannot judge whether the effect
  was *correct*, only whether anything ever went and looked.

The two shapes are one theme — *a side effect nobody confirmed* — and should probably ship as one
rule.

## Exploratory

### Runtime enforcement (a different product, not a rule)

The deterministic rules could run on the *partial* trace as a pre-action guard, not just post-hoc:
refuse a side-effecting call whose structural precondition failed, with no model in the hot path.
Hard constraint — only the `hard_defect` tier could ever gate a live action; a *candidate* blocking
a real action would be worse than the failure it prevents, so the candidate/verdict split becomes
safety-critical rather than a reporting nicety. This is middleware in the agent loop — a different
surface and risk profile than a CI linter. Interesting, not near-term.

### Adapter follow-ups (still deferred)

- **GenAI chat-embedded tool calls** — the `parts`-format `tool_call` / `tool_call_response` blocks,
  for GenAI `chat` spans that carry tool calls inline and emit no `execute_tool` span.
- **In-text tool-call encodings** — e.g. ShareGPT `<tool_call>{…}</tool_call>` inside assistant text.
- **A declarative field-path mapping config**, so a new custom format is ~10 lines rather than code —
  worth building only if the two skeleton readers (message-list, event/span-list) stop covering the
  real formats people actually bring.

## Ecosystem

tracelint composes with **muteval** (mutation testing for evals) via the `muteval[tracelint]` extra:
muteval mutates an agent's tool outputs (e.g. a 200-with-`declined` domain failure) and grades with
`checks.tracelint()` — a deterministic, judge-free eval — so a silent fault the semantic evals miss
is caught by a declared `failure_when` contract. The integration lives in muteval; tracelint needs no
changes for it.
