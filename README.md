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
uv run pytest                 # 28 tests: failure modes, determinism, audit chain
```

## Running a real agent

`examples/llm_agent.py` puts an **actual LLM** behind the gate. The model is given
evidence-gathering tools plus a sensitive `send_marketing` tool it may only call
*with an Evidence Manifest it declares itself*; that manifest is routed through
`gate.check()`. The model never decides whether it is ready to act — the gate does.

```bash
uv run examples/llm_agent.py --mock   # scripted stand-in model — no API, no cost
uv run examples/llm_agent.py          # live, via OpenRouter z-ai/glm-4.7
```

The live path needs `OPENROUTER_API_KEY` in a gitignored `.env`. Point at any
OpenAI-compatible endpoint by overriding `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.

Three contacts drive the three failure modes end-to-end — and the model *wants to
send in every case*, but the gate allows only the one with sound evidence:

```
contact 42  fresh opt-in      -> ALLOW   (executed)
contact 77  14-month-old opt-in-> REVIEW  (ticket queued, not executed)
contact 99  no record          -> BLOCK   (not executed)
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
examples/      # demo_agent.py (fake agent), llm_agent.py (real LLM behind the gate)
tests/         # golden + property tests
```
