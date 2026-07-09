# Evidence Gate

![Evidence Gate — deterministic runtime enforcement for agent tool calls](./assets/banner.svg)

> **Status: alpha (v0.4.1).** The core gate, engine, audit chain, cross-key
> `compare` rules, a real RESTRICT degradation path, and a live LLM agent work
> today; v0.3 added the **integration surface** (remote HTTP gate service,
> fail-closed client with name-pattern instrumentation, HMAC-signed clearance
> tokens, Trace-to-Gate replay, LangChain adapter); v0.4 rounded it out with
> **per-vendor trace presets** (LangSmith / Langfuse / OpenAI), **CrewAI and
> LlamaIndex adapters**, and **hygienic OTel decision telemetry**; and v0.4.1 adds
> the **adoption CLI** (`evidence-gate replay` / `audit verify` / `policy lint` /
> `policy compile`), a **residual-risk coverage report**, **downstream token
> enforcement** (`require_clearance`), a **durable SQLite review queue**
> (`SQLiteReviewQueue`), and the **offline policy compiler** (SOP text → linted,
> human-approved rule pack) — see [Roadmap](#roadmap). APIs are stabilizing but may
> still shift before v1.0.

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

## Architecture

The gate is a **pure decision seam** — `evaluate(action, manifest, policy, now)` —
that everything else plugs into. The agent reaches it one of two ways (in-process,
or over HTTP through the fail-closed client), and the evidence manifest arrives one
of two ways (declared by the agent, or reconstructed from observed tool calls). All
paths converge on the *same* deterministic engine; no LLM ever runs inside it.

```mermaid
flowchart TB
    subgraph agent["Agent runtime"]
        LLM["LLM / tool loop"]
        LC["Framework adapter<br/>(LangChain / CrewAI / LlamaIndex)"]
        LLM -. evidence tools .-> LC
    end

    subgraph adopt["Adoption surface"]
        direction LR
        INPROC["In-process<br/>Gate.check() / @enforce"]
        REMOTE["RemoteGate<br/>(httpx, fail-closed)"]
    end

    subgraph evidence["Evidence manifest — two paths"]
        direction LR
        DECL["Agent-declared<br/>EvidenceManifest"]
        RECON["Reconstructed<br/>ManifestBuilder + extractors"]
    end

    subgraph core["Deterministic core (no LLM)"]
        GATE["Gate.check()<br/>1. structural validation<br/>2. evaluate<br/>3. audit<br/>4. route"]
        ENGINE["PolicyEngine.evaluate()<br/>named primitives · no eval"]
        POLICY[("PolicySet<br/>versioned YAML")]
        GATE --> ENGINE
        POLICY --> ENGINE
    end

    subgraph out["Outputs"]
        direction LR
        VERDICT["ALLOW · RESTRICT<br/>REVIEW · BLOCK"]
        AUDIT[("Hash-chained<br/>audit log")]
        REVIEW[("Review queue<br/>non-blocking")]
        TOKEN["Signed clearance token<br/>(HMAC, ALLOW/RESTRICT)"]
        OTEL["OTel span event<br/>(payload-safe, opt-in)"]
    end

    LC --> INPROC
    LC --> REMOTE
    LLM --> INPROC
    REMOTE -->|POST /v1/check| SVC["FastAPI gate service"]
    SVC --> GATE
    INPROC --> GATE

    DECL --> GATE
    RECON --> GATE

    ENGINE --> VERDICT
    GATE --> AUDIT
    GATE -. telemetry sink .-> OTEL
    VERDICT -->|REVIEW| REVIEW
    VERDICT -->|ALLOW / RESTRICT| TOKEN

    TRACE["Recorded traces<br/>(LangSmith / Langfuse / OpenAI)"] -. normalize + simulate .-> RECON
```

Two invariants hold across every path: the engine is a **pure function** of its
inputs (`now` is injected, never clock-read), and **nothing executes unrecorded** —
the audit record is appended before `check()` returns.

## Quick start

```bash
uv sync                         # install core deps into .venv
uv run examples/demo_agent.py   # marketing tripwire: the four failure modes
uv run examples/refund_agent.py # refund tripwire: cross-key compare + RESTRICT
uv run pytest                   # 159 tests: failure modes, determinism, audit, remote, adapters, telemetry, cli, review, compiler
```

The core gate depends only on `pydantic` + `pyyaml`; `openai`/`python-dotenv` come
along for the live-LLM example. The remote gate and framework adapters are **optional
extras**, so nothing that doesn't need FastAPI/httpx/LangChain pulls them in:

```bash
uv sync --extra service --extra client --extra langchain \
        --extra crewai --extra llamaindex --extra otel        # everything
```

| Extra | Adds | Enables |
|---|---|---|
| `service` | `fastapi`, `uvicorn` | `create_app()` — the HTTP gate service |
| `client`  | `httpx` | `RemoteGate` — the fail-closed remote client |
| `langchain` | `langchain-core` | `EvidenceGateCallbackHandler` |
| `crewai` | `crewai` | `gate_crew_tools()` — gated `BaseTool` wrappers |
| `llamaindex` | `llama-index-core` | `gate_llama_tools()` — gated `FunctionTool` wrappers |
| `otel` | `opentelemetry-api` | `OTelSink` — hygienic `evidence_gate.decision` span events |

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

**Cross-key rules.** A `compare` block relates one evidence key to another key or
a literal threshold — the constraint the per-key requirements can't express:

```yaml
- id: refund_within_order_total
  compare: { left_key: "refund.amount", op: "<=", right_key: "order.total" }
  effect_on_fail: block            # can't refund more than was ever charged
```

**Trace-derived manifests.** Instead of the agent declaring its own manifest, the
gate can assemble one from the tool calls it actually made, via explicit
extractors (deterministic, no LLM):

```python
from evidence_gate import ManifestBuilder, ToolCall

builder = ManifestBuilder().register("get_optin", optin_extractor)
manifest = builder.build(recorded_tool_calls, compiled_at=now)
gate.check(action, manifest)       # evaluated identically to an agent-supplied one
```

## Remote gate (service + fail-closed client)

The same pure `check()` lifts behind FastAPI, so the engine and policies can live in
one place and agents call it over the wire. The client is **fail-closed**: an
unreachable gate never means "allow."

```python
# server — the gate service
from evidence_gate import Gate, PolicySet, Signer, create_app

app = create_app(Gate(PolicySet.from_dir("policies")), signer=Signer(b"secret-key"))
# uvicorn: `create_app` returns a FastAPI app with /v1/check, /v1/review, /v1/audit
```

```python
# client — wrap existing tools by name pattern, no per-call-site rewrite
from evidence_gate import RemoteGate

gate = RemoteGate("https://gate.internal")
gate.auto_instrument(tools, {"stripe_*": "billing.issue_refund"})

tools.stripe_refund(45, "late package")   # gated transparently; runs only on ALLOW/RESTRICT
#   BLOCK            -> raises ClearanceDenied(.reason, .request_id)
#   gate unreachable -> raises GateUnreachable (tool never runs)
```

On `ALLOW`/`RESTRICT` the service returns a short-lived **HMAC-signed clearance
token** (`Signer`/`Verifier`); the client verifies it on receipt. The same key
signs the audit chain — with `key=None` the chain hash is byte-identical to the
plain hash, so signing is a backwards-compatible drop-in.

A downstream service that actually executes the effect can make that token
**load-bearing** — refusing any call that doesn't carry a fresh, action-bound one:

```python
from evidence_gate import Verifier, require_clearance

@require_clearance(Verifier(b"secret-key"), action="billing.issue_refund")
def execute_refund(amount):          # the token is consumed by the guard, not forwarded
    ledger.debit(amount)

execute_refund(45, clearance_token=token)   # runs only if the token verifies for this action
#   missing / forged / expired token -> raises ClearanceRequired (effect never runs)
#   token minted for another action  -> raises ClearanceRequired
```

## Trace-to-Gate (replay recorded traces)

Point the gate at the tool-call log an agent *already* produced and see what it
*would have decided* — the onboarding hook, before wiring anything live.

```python
from evidence_gate import TraceMapping, normalize, simulate

calls = normalize(trace_records, TraceMapping(tool="name", call_id="id",
                                              observed_at="ts", result="data.output")).calls
reports = simulate(calls, gate=gate, builder=builder,
                   action_mapping={"send_*": "marketing.send_sequence"}, now=now)
# -> [SimReport(request_id=..., effect=ALLOW/REVIEW/BLOCK, executed=..., reasons=[...])]
```

`normalize` maps arbitrary vendor exports (dotted field paths) into the `ToolCall`
shape; a record missing a required field is *skipped and surfaced*, never guessed.
For the common vendors this is a one-liner — ship-ready `TraceMapping` presets
cover their per-observation shapes:

```python
from evidence_gate import LANGSMITH, LANGFUSE, OPENAI, normalize

calls = normalize(runs, LANGSMITH).calls        # or LANGFUSE, or OPENAI
```

`simulate` scopes evidence per turn and runs every sensitive call through the
untouched `gate.check()`. See `examples/trace_replay.py`.

## CLI (`evidence-gate`)

The replay, audit-verify, and policy-authoring flows, from a shell — the Phase-1
onboarding surface, no Python required beyond an importable extractor module.

```bash
# Diagnose: what would the gate have decided on yesterday's trace?
evidence-gate replay trace.json \
  --policy policies --mapping langsmith \
  --action 'send_*=marketing.send_sequence' \
  --extractor 'get_optin=myapp.extractors:optin'
#   s42  ALLOW  executed=true
#   s99  BLOCK  executed=false — missing required evidence for 'marketing.opt_in'
#   Coverage:  gated: send_marketing | ! UNCLASSIFIED: wire_transfer (x1)

# Verify a hash-chained audit log written by AuditLog(path=...)
evidence-gate audit verify audit.jsonl        # exit 0 = intact, 1 = tampered

# Lint a candidate policy against the engine schema (no LLM, no write)
evidence-gate policy lint draft.yaml          # exit 0 = clean, 1 = findings by field

# Author: draft a policy from an SOP via an LLM, then require sign-off to activate
evidence-gate policy compile sop.txt --approve alice --out policies
#   drafted -> linted -> approved(alice) -> wrote policies/<name>.yaml
```

`--mapping` takes a preset (`langsmith` / `langfuse` / `openai`) or a
`TraceMapping` JSON file; `--extractor TOOL=module:function` registers an evidence
extractor (uvicorn-style import). Every `replay` run prints a **coverage** section
that names any *unclassified* tool — a call reached in the trace that no `--action`
or `--extractor` accounts for — so residual risk is surfaced, never silently missed.
`--now` injects the evaluation time for reproducible runs.

`policy compile` is **offline authoring only** — it never runs on the gate's
decision path, and `--approve` is mandatory: a drafted-but-unapproved policy cannot
reach enforcement. The draft is linted against the exact `Policy` schema the engine
consumes, so anything that activates is something the engine can run.

## Framework adapters

Three adapters share one seam — a `GatePort` (local or remote) plus a `GateSession`
that accumulates evidence as tools run. Each gates the sensitive tool *in the
agent's own call path*, so `BLOCK`/`REVIEW` raise `ClearanceDenied` before it
executes; all three work against an in-process `Gate` or the remote client
unchanged.

**LangChain** — a callback handler that collects evidence tools and gates the
sensitive one:

```python
from evidence_gate import Gate, ManifestBuilder, PolicySet
from evidence_gate.integrations.langchain import EvidenceGateCallbackHandler, LocalGatePort

port = LocalGatePort(Gate(PolicySet.from_dir("policies")), builder)   # or RemoteGatePort(RemoteGate(...))
handler = EvidenceGateCallbackHandler(port, action_mapping={"send_*": "marketing.send_sequence"})
agent.invoke(..., config={"callbacks": [handler]})   # BLOCK/REVIEW raise before the tool runs
```

**CrewAI / LlamaIndex** — both frameworks' event buses are observe-only (they
can't stop a call), so these adapters wrap the tool itself and share a `GateSession`:

```python
from evidence_gate.integrations.base import GateSession, LocalGatePort
from evidence_gate.integrations.crewai import gate_crew_tools        # or llamaindex.gate_llama_tools

session = GateSession(LocalGatePort(gate, builder),
                      action_mapping={"send_*": "marketing.send_sequence"}, now=now)
tools = gate_crew_tools([get_optin, send_marketing], session)        # gated, drop-in replacements
```

See `examples/remote_agent.py`, `examples/trace_replay.py`, and
`examples/langchain_agent.py` for each path end-to-end.

## Decision telemetry (OTel, payload-safe)

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

## Layout

```
evidence_gate/
  schemas.py         # EvidenceItem, EvidenceManifest, ProposedAction, Decision
  policy.py          # typed rule models (incl. Comparison) + YAML loader
  engine.py          # evaluate() — pure, deterministic
  gate.py            # Gate.check() + @enforce decorator
  audit.py           # hash-chained append-only log
  review.py          # human-in-the-loop routing
  trace.py           # ManifestBuilder — derive a manifest from tool-call traces
  trace_adapters.py  # normalize() + simulate() + coverage() + vendor presets
  signing.py         # HMAC Signer/Verifier + require_clearance downstream guard
  cli.py             # evidence-gate: replay + audit verify   [console entrypoint]
  telemetry.py       # DecisionEvent + OTelSink — payload-safe span events [extra: otel]
  service.py         # create_app() — FastAPI gate service        [extra: service]
  client.py          # RemoteGate — fail-closed client            [extra: client]
  integrations/
    base.py          # GatePort / GateSession seam shared by all adapters
    langchain.py     # EvidenceGateCallbackHandler                 [extra: langchain]
    crewai.py        # gate_crew_tools() — gated BaseTool wrappers  [extra: crewai]
    llamaindex.py    # gate_llama_tools() — gated FunctionTool wrappers [extra: llamaindex]
policies/            # marketing.yaml, refund.yaml
examples/            # demo_agent, refund_agent (RESTRICT), llm_agent (real LLM),
                     # remote_agent, trace_replay, langchain_agent
tests/               # golden + property + integration tests (159)
```

## Roadmap

**Core — working since v0.2.0**

- [x] Typed schemas — `EvidenceItem` / `EvidenceManifest` / `ProposedAction` / `Decision`
- [x] Deterministic policy engine — pure `evaluate(action, manifest, policy, now)`
- [x] The seven requirement primitives + the four failure modes (`missing` / `stale`
      / `conflicting` / `unauthorized`)
- [x] `Gate.check()` + `@enforce` decorator; deny-by-default for ungoverned actions
- [x] Hash-chained, tamper-evident audit log; audited human-review resolution
- [x] In-memory review queue that never breaks the agent loop
- [x] Real LLM agent (`examples/llm_agent.py`) driving the gate end-to-end
- [x] **`RESTRICT` execution path** — large refund → capped partial (`examples/refund_agent.py`)
- [x] **Cross-key `compare` rules** — `refund.amount ≤ order.total` (DESIGN §12.4)
- [x] **Trace-derived manifests** — `ManifestBuilder` from recorded tool calls (DESIGN §12.1)

**Integration surface — new in v0.3.0**

- [x] **HTTP gate service** — `create_app()`, a FastAPI wrapper over `gate.check()`
      with both manifest paths (`service.py`, extra `service`)
- [x] **Fail-closed remote client** — `RemoteGate` with name-pattern
      `auto_instrument`, `ClearanceDenied` / `GateUnreachable` (`client.py`, extra `client`)
- [x] **Real-key signing** — HMAC `Signer`/`Verifier`; signed clearance tokens on
      ALLOW/RESTRICT; backwards-compatible audit-chain hash (`signing.py`)
- [x] **Trace-to-Gate replay** — generic `normalize()` + `simulate()` over the
      `ManifestBuilder` seam (`trace_adapters.py`)
- [x] **LangChain adapter** — `EvidenceGateCallbackHandler` over a local/remote
      `GatePort` seam (`integrations/langchain.py`, extra `langchain`)
- [x] 89 golden + property + integration tests

**Adapters, presets & telemetry — new in v0.4.0**

- [x] **Per-vendor trace presets** — `LANGSMITH` / `LANGFUSE` / `OPENAI`
      `TraceMapping`s over the generic seam (`trace_adapters.py`).
- [x] **CrewAI + LlamaIndex adapters** — `gate_crew_tools` / `gate_llama_tools`
      over the shared `GatePort` / `GateSession` seam (`integrations/`).
- [x] **OTel span events** — `evidence_gate.decision` / `.pending_review` via
      `OTelSink`, excluding raw args/prompt/model output (`telemetry.py`).
- [x] 110 golden + property + integration tests

**Adoption CLI, coverage & downstream enforcement — new in v0.4.1**

- [x] **CLI entrypoint** — `evidence-gate replay <trace> …` and
      `evidence-gate audit verify <log>` over the existing `simulate`/`AuditLog`
      seams (the Phase-1 "CLI" surface); `cli.py`, `[project.scripts]`.
- [x] **Residual-risk / coverage report** — `coverage()` classifies every tool in a
      trace as gated / recognized-evidence / **unclassified**, surfacing
      known-unwrapped tools by name; folded into `replay` output.
- [x] **Downstream token enforcement** — `require_clearance(verifier, action=…)`
      refuses any downstream call lacking a fresh, action-bound clearance token;
      Verify is now load-bearing, not decorative (`signing.py`).
- [x] **Persistent review backend** — SQLite-backed `SQLiteReviewQueue` that survives
      restarts and is safe to share across threads and processes (`review.py`).
- [x] **Offline policy compiler** — LLM-driven draft generator (`compiler.py`) from
      plain-text SOP with static semantic linter and mandatory sign-off workflow
      (`policy compile` / `policy lint`).

**Pending for the alpha line (next)**

- [ ] **Key rotation / revocation** — support a `kid` header so multiple `Signer`
      keys validate during rotation; document rotate + revoke.

**Deliberately deferred** (see [`DESIGN.md`](./DESIGN.md) §9)

- [ ] **Trace ingestion → candidate rules** (distinct from replay: mining logs to
      *propose* policy, not just simulate it).
- [ ] **Asymmetric signing** — swap HMAC for public-key so verifiers need no shared
      secret; a drop-in over the current `Signer` (DESIGN §13.5).
- [ ] **RBAC/ABAC** — assumed upstream; the gate is orthogonal and additive.

## Release notes

### v0.4.1 alpha — adoption CLI, coverage & downstream enforcement

Three additions that make the *simulate → enforce* funnel walk-able end-to-end
from a shell, and make the clearance token actually load-bearing — all still thin
plumbing over the pure `check()` / `simulate()` / `Signer` seams:

- **Adoption CLI.** `evidence-gate replay` runs a recorded trace through the gate
  and prints the per-call verdicts (the Diagnose output); `evidence-gate audit
  verify` recomputes a JSONL audit chain and exits non-zero if it was tampered.
- **Residual-risk coverage.** `coverage()` classifies every tool in a trace as
  gated, recognized-evidence, or **unclassified** — the last being a route no
  policy or extractor accounts for. Surfaced by name in the `replay` output, so a
  coverage gap reads as a warning, never a silent miss (vision §5.6).
- **Downstream token enforcement.** `require_clearance(verifier, action=…)` guards
  a downstream effect so it refuses any call without a fresh, action-bound
  clearance token — a token minted for another action, forged, or expired is
  rejected and the effect never runs. Verify is now enforcement, not decoration.
- **Persistent review backend.** SQLite-backed `SQLiteReviewQueue` that survives
  restarts and is safe to share across threads and processes (`review.py`).
- **Offline policy compiler.** LLM-driven draft generator (`compiler.py`) from
  plain-text SOP with static semantic linter and mandatory sign-off workflow
  (`policy compile` / `policy lint`).

Verified: 159 tests pass; the base package still
imports with no extra; `replay` and `audit verify` run end-to-end; the guard
fails closed on missing / forged / expired / wrong-action tokens.

### v0.4.0 alpha — adapters, presets & telemetry

v0.3.0 opened the integration surface with one framework adapter and a generic
trace mapping; v0.4.0 fills it in, still without touching the pure `check()` seam:

- **Per-vendor trace presets.** `LANGSMITH` / `LANGFUSE` / `OPENAI` are ready-made
  `TraceMapping`s for each vendor's per-observation shape — replay a real export
  with `normalize(runs, LANGSMITH)` instead of hand-mapping dotted paths.
- **Two more framework adapters.** `gate_crew_tools` (CrewAI) and `gate_llama_tools`
  (LlamaIndex) join the LangChain handler over a shared `GatePort` / `GateSession`
  seam. Both frameworks' event buses are observe-only, so each adapter gates the
  tool *in the agent's own call path* — `BLOCK`/`REVIEW` raise before it runs.
- **Payload-safe decision telemetry.** `OTelSink` adds an `evidence_gate.decision`
  (or `.pending_review`) event to the active span carrying only the verdict, rules
  fired, and evidence *keys/counts* — never args, prompts, model output, or
  evidence values. Opt-in (`NullSink` by default), no-op without the `otel` extra.

Verified: 110 tests pass (the 89 from v0.3 unchanged); the hygiene boundary is
tested independently of OTel; every adapter stops a `BLOCK`/`REVIEW` in the agent's
own call path.

### v0.3.0 alpha — the integration surface

v0.2.0 proved the thesis in-process; v0.3.0 makes it **adoptable** — the goal was
to close the gap between "a correct engine" and "something an agent builder can put
in front of a real tool without rewriting their stack," while keeping the engine
untouched. Everything below is plumbing over the pure `check()` seam:

- **Remote, fail-closed enforcement.** `create_app()` serves the gate over HTTP;
  `RemoteGate` calls it and refuses to execute when the gate is unreachable — an
  unavailable gate never silently allows.
- **Zero-touch instrumentation.** `RemoteGate.auto_instrument(tools, {"stripe_*":
  ...})` wraps existing tools by name pattern; call sites don't change.
- **Signed clearance.** HMAC `Signer`/`Verifier` issues short-lived tokens on
  ALLOW/RESTRICT and — with `key=None` — keeps the audit-chain hash byte-identical,
  so signing is a backwards-compatible drop-in.
- **Trace-to-Gate onboarding.** `normalize()` + `simulate()` replay a recorded
  trace through the gate to show what it *would have decided*, before wiring live.
- **Framework adapter.** `EvidenceGateCallbackHandler` gates a LangChain agent's
  sensitive tool call, working against the local `Gate` or the remote client via
  one `GatePort` seam.

Verified: 89 tests pass (45 original, unchanged); the audit chain is regression-clean
(unsigned == identical hashes); the base package imports without any extra; and all
three new examples run end-to-end (both manifest paths agree; fail-closed raises
without executing).

### v0.2.0 alpha — the core

The first version coherent enough to hand to someone else and have them gate a real
action end-to-end. The bar for "shippable" was: **every part of the thesis is
exercised by a runnable example and pinned by a test — no stubs on the critical
path.** What that means concretely:

- **A real agent can drive it.** `examples/llm_agent.py` puts a live LLM behind
  the gate; the model gathers evidence and *wants* to send in every case, and the
  gate — not the model — decides ALLOW / REVIEW / BLOCK.
- **All four verdicts execute, not just three.** `RESTRICT` is no longer a stub:
  `examples/refund_agent.py` degrades an over-ceiling refund to a capped partial
  and hard-BLOCKs a refund exceeding the order total.
- **Rules can span keys.** The `compare` primitive expresses
  `refund.amount ≤ order.total` and literal thresholds as named, `eval`-free
  operators.
- **Manifests can come from execution, not just self-report.** `ManifestBuilder`
  derives an `EvidenceManifest` from recorded tool-call traces via explicit
  extractors, converging on the exact schema the gate already evaluates.
- **Everything is recorded and reproducible.** Every `check()` appends a
  hash-chained audit record; the engine is a pure function of
  `(action, manifest, policy, now)`; 45 golden + property tests lock the four
  failure modes, aggregation, determinism, and chain integrity.

The v0.2.0 ship boundary called out four seams left open — the HTTP service,
real-key signing, the trace-log adapter, and the offline policy compiler. The first
three shipped in v0.3.0 (above); the offline compiler remains deferred (see
[Roadmap](#roadmap) and [`DESIGN.md`](./DESIGN.md) §9). APIs are stabilizing but may
still shift before v1.0.
