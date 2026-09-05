# tracelint + OpenAI message lists (and ShareGPT)

Not every team has an OTel/observability stack — sometimes you just have the **chat message list**
your agent sent to and got back from the model. tracelint reads that directly, no tracing setup
required. This is the lowest-friction ingestion path.

## Lint a message list

```bash
tracelint check messages.json --format openai
```

`.jsonl` is accepted too (one message per line).

## What tracelint reads

- `system` / `user` / `assistant` text → messages;
- an assistant message's `tool_calls[]` → tool calls (OpenAI encodes `arguments` as a JSON string;
  tracelint parses it into the args R1 validates, keeping the raw string as evidence if it won't
  parse);
- a `role: "tool"` message → a tool result, paired by `tool_call_id`.

**Real-world variants are handled without a bespoke adapter** — the variation is field names and
encoding, not shape:

- **role key**: `role` (OpenAI) or `from` (ShareGPT — `human`/`gpt` → `user`/`assistant`);
- **content key**: `content` (OpenAI), `value` (ShareGPT), or `text` (some trajectory dumps);
- **content as typed blocks**: a plain string, or the `[{"type": "text", "text": ...}]` list form —
  flattened to text for messages, while a *structured* tool-result payload (a dict/list) is kept
  intact so R2 / `failure_when` can read it.

So a **ShareGPT-format dataset** of agent transcripts lints with the same command.

## Note on schemas

A raw message list carries no tool JSON Schemas, so pass a `tools.json` (`--tools`) for R1 and the
behavioral rules; the ground-truth-free rules (tool errors, malformed args, loops) run without it.

## Scope

The message-list reader is the zero-infrastructure entry point — useful for linting transcripts and
datasets when there is no OTel/observability backend in the loop.
