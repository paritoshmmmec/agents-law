# Usage & policies

```python
from evidence_gate import Effect, Gate, PolicySet

gate = Gate(PolicySet.from_dir("policies"))

result = gate.check(action, manifest)   # deterministic verdict + audit record
if result.allowed:                       # ALLOW or RESTRICT
    tool.execute(action.payload)
```

Or wrap a tool function directly. On `RESTRICT` the tool still runs but is told
so via `effect=`, and degrades its own payload:

```python
@gate.enforce(action="billing.issue_refund")
def issue_refund(payload, effect):
    amount = payload["refund_amount"]
    if effect is Effect.RESTRICT:          # over the auto-approve ceiling
        amount = min(amount, 5000)         # execute a capped partial, not the full ask
    ...  # runs only on ALLOW/RESTRICT; raises ActionBlocked on BLOCK;
         # returns a pending GateResult on REVIEW
```

## Cross-key rules

A `compare` block relates one evidence key to another key or a literal threshold —
the constraint the per-key requirements can't express:

```yaml
- id: refund_within_order_total
  compare: { left_key: "refund.amount", op: "<=", right_key: "order.total" }
  effect_on_fail: block            # can't refund more than was ever charged
```

## Trace-derived manifests

Instead of the agent declaring its own manifest, the gate can assemble one from the
tool calls it actually made, via explicit extractors (deterministic, no LLM):

```python
from evidence_gate import ManifestBuilder, ToolCall

builder = ManifestBuilder().register("get_optin", optin_extractor)
manifest = builder.build(recorded_tool_calls, compiled_at=now)
gate.check(action, manifest)       # evaluated identically to an agent-supplied one
```
