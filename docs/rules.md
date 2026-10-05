# Rule reference

Every finding tracelint reports names a rule (`R1`, `R2b`, …). This page says, for each one, what
it checks, which tier it surfaces at, and what to do about it. The text report and the SARIF
`helpUri` link here, one anchor per rule.

## Tiers

A finding's **tier** is how sure tracelint is, not how bad the problem is:

- **`hard_defect`** — structurally provable from the trace. This is the tier that fails CI (exit 2).
- **`hard_event`** — a certain fact (a tool errored, a side effect repeated), surfaced for review.
- **`candidate`** — a heuristic match. Shown for a human to judge; never fails CI on its own.

One finding kind can surface at more than one tier depending on the evidence — the per-rule notes
below say when.

## Acting on a finding

For each finding you have four honest moves:

1. **Fix the agent** — the finding is real; change the agent so it stops happening.
2. **Declare a fact** in `tools.json` so the rule can judge correctly (e.g. `failure_when`,
   `side_effecting`, `idempotent`, `requires`, an output schema). `tracelint init` scaffolds it.
3. **Ignore** it with a reason (`# tracelint: ignore[R4] <why>`-style config) when it is known and
   accepted — it stays visible but stops failing the run.
4. **Suppress** is not a move you make; it is tracelint telling you a rule *could not* run here
   (no contract, or the trace didn't record what it needed). A suppressed rule is not a clean pass.

---

<a id="r1"></a>
## R1 — schema violation

**Tier:** `hard_defect`

A tool call's arguments don't satisfy the tool's declared JSON Schema — a required field is
missing, a value is the wrong type, an enum is violated. Provable whenever a schema is known (from
`tools.json`, or carried in the trace).

**What to do:** fix how the agent builds the arguments, or correct the schema if the schema is the
thing that's wrong. Needs a schema to run — if none is declared, R1 is suppressed, not passed.

<a id="r2a"></a>
## R2a — tool error

**Tier:** `hard_event` (a structured error signal) / `candidate` (a heuristic match)

A tool returned an error: an error status on the span or run, a LangChain `ToolMessage` with
`status: "error"`, a non-empty `error` field, or (as a candidate) an exception-like string. Fields
*inside* a result such as `status_code: 404` are the tool's own data, not an error, until
`failure_when` declares what failure looks like for that tool.

**What to do:** make the agent handle or retry the error. If the "error" is really expected data,
declare `failure_when` so tracelint stops flagging it.

<a id="r2b"></a>
## R2b — error consumed

**Tier:** `hard_defect` / `candidate`

A value that *only* a failed result supplied is then used by a later side-effecting call — the
agent acted on output from a call it should have treated as failed. R2b follows the data, not
repeated text: a value counts only when nothing else the agent saw could have supplied it. If the
value merely passes through a later lookup that may have confirmed it, the use is a `candidate`.

**What to do:** gate the side-effecting call on the earlier call succeeding; don't feed a failed
call's output forward. An action that must never run after a failed check is better declared as a
precondition — see [R9](#r9).

<a id="r3"></a>
## R3 — hallucinated argument

**Tier:** `candidate`; `hard_defect` when the field is annotated `provided`

An argument value isn't derivable from anything the agent observed earlier in the trace — not from
the user, not from a prior tool result. A candidate by default; a hard defect when the contract
annotates the field as one that must be `provided` from observed data.

**What to do:** ground the argument in something the agent actually saw, or annotate the field's
provenance in the contract so the high-confidence tier can apply.

<a id="r4"></a>
## R4 — loop

**Tier:** `candidate`

The same call repeats with no change in state between repeats. Deliberate retries and polling are
excluded — this is a stuck agent, not a legitimate wait.

**What to do:** add a termination condition or make each iteration make progress.

<a id="r5"></a>
## R5 — redundant call

**Tier:** `candidate`

An identical call returns an identical result with no state change in between — wasted work, not a
loop.

**What to do:** cache the first result or drop the duplicate call.

<a id="r6"></a>
## R6 — malformed arguments

**Tier:** `hard_defect` when the model emitted them; `candidate` in a tool's own record

The tool-call arguments aren't well-formed against the call contract — e.g. not valid JSON. A hard
defect when it's the model's emitted function call; a candidate when it's a tool's own recorded
arguments (which may be lossy).

**What to do:** fix the model's function-call emission (often a prompt or tool-definition issue).

<a id="r7"></a>
## R7 — unknown tool

**Tier:** `candidate`

A call to a tool that isn't in the declared toolset — possibly a hallucinated tool, possibly just a
tool you forgot to declare. Its behavior is unverified, so no other rule can reason about it.

**What to do:** add the tool to `tools.json` if it's real, or investigate it as a hallucinated tool
if it isn't.

<a id="r8"></a>
## R8 — duplicate side effect

**Tier:** `hard_event`; `candidate` if the first result is unknown

A non-idempotent side-effecting call repeated with equivalent arguments *after the first one
succeeded* — the kind of bug that charges a card twice or sends two emails.

**What to do:** give the operation an idempotency key, or dedupe before calling. If the tool is
genuinely idempotent, mark it `idempotent` in the contract and R8 won't fire.

<a id="r9"></a>
## R9 — unmet precondition

**Tier:** `hard_defect` (declared in `tools.json`)

A tool ran although a call its contract `requires` had failed, or had not returned, first — e.g.
`deploy` that `requires` a passing `run_pipeline`. Only fires for preconditions you declare.

**What to do:** order the calls so the precondition is satisfied, or gate the dependent call on the
required one succeeding. Declare the dependency with `requires` in the tool's contract.

<a id="r10"></a>
## R10 — result contract

**Tier:** `hard_event` (opt-in; only tools that declare an `output_schema`)

A tool's *result* doesn't satisfy the output JSON Schema its contract declares — for instance an
MCP tool's `outputSchema`. Dormant unless a tool declares an output schema.

**What to do:** fix the tool so its result matches the declared shape, or correct the output schema
if the schema is wrong.

<a id="r11"></a>
## R11 — contract drift

**Tier:** `hard_event` (opt-in; needs both an inline and a committed schema)

A tool's schema carried in the trace differs from the committed `tools.json` contract — a field
added, removed, retyped, or made (non-)required since the contract was written. The trace and the
contract disagree about the tool.

**What to do:** reconcile them — re-run `tracelint init` to refresh the contract if the tool
legitimately changed, or fix whichever side is stale.

<a id="r12"></a>
## R12 — unresolved side effect

**Tier:** `candidate` (opt-in; per side-effecting tool)

A side-effecting call whose outcome the trace never resolved: no result was recorded, or it failed
and nothing recovered. You can't tell whether the side effect happened — which, for a non-idempotent
action, is its own risk. A finding at the very end of a trace is flagged `possible_false_positive`
(the trace may simply have been cut off).

**What to do:** record the call's result so the outcome is known, or handle the failure explicitly.
