# Tool Contracts

tracelint never guesses a tool's behaviour from its name. What a tool does — whether it mutates the
world, what "failure" means for it, where its arguments must come from — is **declared** by the
operator, in `tools.json`, and the rules check the recorded execution against that declaration.

Those declarations have accumulated as separate metadata keys. They are, though, one idea: **the
contract the tool operates under.** A `ToolContract` is just a coherent view over the keys you
already write — it adds no new fields and changes no behaviour.

## The sections

A contract has five parts, each backed by a declaration:

| Section | Declared by | What it tells the rules |
|---|---|---|
| **args** | `schema` (JSON Schema) | R1 replays each recorded call against it. |
| **effects** | `metadata.side_effecting` (+ `idempotent`, `polling`, `paginated`) | R2/R4/R5: which calls mutate the world, which repetition is legitimate. |
| **failure** | `metadata.failure_when` (a JSON-pointer predicate) | R2: a domain failure returned as a transport success (HTTP 200 + `{"status":"declined"}`). |
| **provenance** | per-field `x-value-origin` (`provided` / `generated`) | R3: whether a value could be a hallucination. |
| **preconditions** | `metadata.requires` (calls that must succeed first) | R9: an action that ran although a call it requires had failed, or had not returned, first. |

## A full example

```json
{
  "tools": {
    "charge_card": {
      "schema": {
        "type": "object",
        "properties": {
          "account_id": { "type": "string", "x-value-origin": "provided" },
          "request_id": { "type": "string", "x-value-origin": "generated" }
        },
        "required": ["account_id"]
      },
      "metadata": {
        "side_effecting": true,
        "failure_when": { "pointer": "/status", "in": ["declined", "failed"] }
      }
    }
  }
}
```

The pointer reads the tool's own result, the same way from every source: tracelint first unwraps a
LangChain `ToolMessage` (as OpenInference, Langfuse and LangSmith record it) and parses a result
recorded as a JSON string (as OpenAI tool messages carry it), so `/status` reaches `"declined"`
wherever the trace came from.

Read it back as one coherent contract:

```python
from tracelint import ToolRegistry

reg = ToolRegistry.load("tools.json")
print(reg.contract_for("charge_card").describe())
```

```text
charge_card
  args:       schema declared (2 properties)
  effects:    side-effecting
  failure:    /status in ['declined', 'failed']
  requires:   none declared
  provenance: account_id=provided, request_id=generated
```

`registry.contracts()` returns the same view for every declared tool, and `.to_dict()` gives a
JSON-friendly form for tooling.

## Preconditions

R2b proves an agent acted on a failed result only when a value flows from the failure into the
action. A refund after a failed lookup of an order id the user gave, or a deploy of `"latest"` after
an `UNSTABLE` pipeline, takes nothing from the failure, so only a declared precondition makes it a
defect:

```json
"refund_order": {
  "metadata": {
    "side_effecting": true,
    "requires": [{"tool": "get_order", "same": ["order_id"]}]
  }
}
```

- **The latest call decides.** Before `refund_order` runs, the latest `get_order` must have returned
  successfully. A retry that passes satisfies it; a later failure un-satisfies it.
- **`same`** (optional) names arguments that must match between the two calls, so refunding order B
  needs B's lookup, not A's.
- **A call still in flight doesn't count.** Firing the lookup and the refund in parallel, without
  waiting for the result, is not checking first.
- **`"succeeded": false`** asks only that the call returned, whatever its outcome.
- **Success is read like R2a reads it:** a structured error or a matching `failure_when` is a
  failure; a `failure_when` that resolves to no match, or an explicit OK, is a success. Anything
  else can't be verified, so the check is disclosed as not run, never passed. Declare
  `failure_when` on the required tool to make it decidable.

A violation is a `hard_defect`. A malformed `requires` entry fails the run (exit 3) rather than
being dropped. `tracelint init` adds a `requires` suggestion to the `_todo` of a tool that was
called after others.

## What this is (and isn't)

- **A presentation, not a new language.** Every section maps to a key that already existed; a tool
  with only some sections declared is fine — the rest read as *none declared*, and the relevant
  rules abstain (see verification coverage) rather than guess.
- **The declaration lives with the operator.** It works for third-party tools whose authors declared
  nothing — you write the contract, tracelint checks the trace against it.
- **Deliberately small.** The contract vocabulary grows only when real usage needs it, not because a
  field sounds like a logical addition. See `ROADMAP.md`.
