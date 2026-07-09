# Decision telemetry (OTel, payload-safe)

Every `check()` can emit one span event describing *what the gate decided* — never
*what the agent was doing*. The event carries only non-sensitive scalars (action
id, verdict, policy version, which rules fired, evidence *keys* and *counts*) and
**never** the payload args, prompt, model output, or an evidence item's value or
claim — exactly what a leak would expose and what a dashboard doesn't need.

```python
from evidence_gate import Gate, OTelSink, PolicySet

gate = Gate(PolicySet.from_dir("policies"), telemetry=OTelSink())   # opt-in; NullSink by default
# adds `evidence_gate.decision` / `evidence_gate.pending_review` to the active span.
```

`OTelSink` is a silent no-op when OpenTelemetry isn't installed or no span is
recording, so wiring it in never requires the `otel` extra and a failing sink never
fails a check. The hygiene boundary (`DecisionEvent.from_decision`) is pure and
unit-tested independently of OTel.
