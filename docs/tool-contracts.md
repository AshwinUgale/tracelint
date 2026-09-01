# Tool Contracts

tracelint never guesses a tool's behaviour from its name. What a tool does — whether it mutates the
world, what "failure" means for it, where its arguments must come from — is **declared** by the
operator, in `tools.json`, and the rules check the recorded execution against that declaration.

Those declarations have accumulated as separate metadata keys. They are, though, one idea: **the
contract the tool operates under.** A `ToolContract` is just a coherent view over the keys you
already write — it adds no new fields and changes no behaviour.

## The four sections

A contract has four parts, each backed by an existing declaration:

| Section | Declared by | What it tells the rules |
|---|---|---|
| **args** | `schema` (JSON Schema) | R1 replays each recorded call against it. |
| **effects** | `metadata.side_effecting` (+ `idempotent`, `polling`, `paginated`) | R2/R4/R5: which calls mutate the world, which repetition is legitimate. |
| **failure** | `metadata.failure_when` (a JSON-pointer predicate) | R2: a domain failure returned as a transport success (HTTP 200 + `{"status":"declined"}`). |
| **provenance** | per-field `x-value-origin` (`provided` / `generated`) | R3: whether a value could be a hallucination. |

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
  provenance: account_id=provided, request_id=generated
```

`registry.contracts()` returns the same view for every declared tool, and `.to_dict()` gives a
JSON-friendly form for tooling.

## What this is (and isn't)

- **A presentation, not a new language.** Every section maps to a key that already existed; a tool
  with only some sections declared is fine — the rest read as *none declared*, and the relevant
  rules abstain (see verification coverage) rather than guess.
- **The declaration lives with the operator.** It works for third-party tools whose authors declared
  nothing — you write the contract, tracelint checks the trace against it.
- **Deliberately small.** The contract vocabulary grows only when real usage needs it, not because a
  field sounds like a logical addition. See `ROADMAP.md`.
