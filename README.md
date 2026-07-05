# Evidence Gate

A deterministic runtime enforcement layer that forces an agent to prove its
reasoning against ground-truth evidence **before** a state-changing action is
committed.

RBAC answers *"can this agent call this tool?"* It never answers *"is the
reasoning behind **this** call sound?"* An agent that hallucinates a request or
acts on stale data still emits a valid, authorized payload — a high-confidence
bad decision. The Evidence Gate closes that hole: it sits on the tool-call path,
demands an **Evidence Manifest** for every sensitive action, and evaluates that
evidence against explicit rules with **no LLM in the loop**.

See [`DESIGN.md`](./DESIGN.md) for the full architecture and
[`problem.md`](./problem.md) for the original requirements.

## Quick start

```bash
uv sync                       # install deps into .venv
uv run examples/demo_agent.py # watch the marketing tripwire in action
uv run pytest                 # 27 tests: failure modes, determinism, audit chain
```

## How it works

1. The agent proposes an action and **declares the evidence** behind it
   (`ProposedAction` + `EvidenceManifest`).
2. The gate runs **structural validation** — no manifest, no action.
3. The **policy engine** deterministically checks the evidence against a
   versioned YAML rule pack and returns one of `ALLOW / RESTRICT / REVIEW /
   BLOCK`.
4. On `REVIEW`, the full context is parked in a review queue **without breaking
   the agent loop**; on `BLOCK` the action is refused.
5. Every decision is written to a **hash-chained, tamper-evident audit log**.

The four evidence failure modes map to deliberate verdicts:

| Failure | Example | Default verdict |
|---|---|---|
| **Missing** | required fact never retrieved | `BLOCK` |
| **Stale** | opt-in older than the policy window | `REVIEW` |
| **Conflicting** | two sources disagree | `REVIEW` |
| **Unauthorized** | fact inferred, not observed | `BLOCK` |

## Usage

```python
from evidence_gate import Gate, PolicySet

gate = Gate(PolicySet.from_dir("policies"))

result = gate.check(action, manifest)   # deterministic verdict + audit record
if result.allowed:                       # ALLOW or RESTRICT
    tool.execute(action.payload)
```

Or wrap a tool function directly:

```python
@gate.enforce(action="marketing.send_sequence")
def send_sequence(payload, effect):
    ...  # runs only on ALLOW/RESTRICT; raises ActionBlocked on BLOCK;
         # returns a pending GateResult on REVIEW
```

## Layout

```
evidence_gate/
  schemas.py   # EvidenceItem, EvidenceManifest, ProposedAction, Decision
  policy.py    # typed rule models + YAML loader
  engine.py    # evaluate() — pure, deterministic
  gate.py      # Gate.check() + @enforce decorator
  audit.py     # hash-chained append-only log
  review.py    # human-in-the-loop routing
policies/      # marketing.yaml, refund.yaml
examples/      # demo_agent.py
tests/         # golden + property tests
```
