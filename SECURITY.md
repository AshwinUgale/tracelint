# Security policy

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository, or contact the
maintainer directly, rather than filing a public issue. We aim to acknowledge
within a few days.

## Threat model & trust boundaries

tracelint is a developer tool you run locally or in your own CI.

- **It reads agent traces as data, not code.** `tracelint check` parses trace files
  (JSON) and lints them; it never executes them. Treat trace files as you would any data
  you load — they may contain content copied from tool outputs or user input.
- **Traces can contain sensitive data.** Tool arguments and results in a real trace may
  include secrets or PII. tracelint processes traces locally and does not send them
  anywhere; the Langfuse integration (`[langfuse]`, opt-in) is the only path that talks to
  a remote service, and only when you configure it.
- **The linter never calls a model.** Detection is deterministic by design — no second
  model judges the trace. The `[real-agent]` extra can *generate* traces (the system under
  test), but that is separate from the linter and opt-in.

If you find a way to make tracelint emit a confident but wrong finding — e.g. assert a
defect on a legitimate retry/poll, or pass a partial trace as fully checked — that's a
security-relevant honesty bug; please report it.
