# Evidence Gate — Design Document

> A deterministic runtime enforcement layer that forces an agent to prove its
> reasoning against ground-truth evidence before a state-changing action is
> committed.

Status: **Draft for review** · Stack: **Python (managed with `uv`)** · Rules:
**own typed schema** · Integration: **in-process SDK wrapper**

---

## 1. Problem & thesis

Standard authorization (RBAC/ABAC) answers *"is this agent permitted to call
this tool?"* It never answers *"is the reasoning behind **this specific call**
sound?"* An agent that hallucinates a request, acts on stale context, or
silently resolves conflicting data still emits a perfectly formatted, authorized
payload. The result is a **high-confidence execution of a bad business
decision**.

**Thesis:** the tool call can be right while the *evidence behind it* is wrong.
We need a non-probabilistic boundary in the execution path that:

1. refuses to run any state-changing action without an **Evidence Manifest**,
2. evaluates that manifest against **explicit, deterministic rules**, and
3. returns one of `ALLOW / RESTRICT / REVIEW / BLOCK`, recording everything.

The LLM compiles the payload and declares the evidence. The LLM **never** decides
whether it is ready to act.

---

## 2. Design constraints → components

Mapping each requirement in `problem.md` to a concrete part of the system.

| # | Requirement | Component | Guarantee |
|---|---|---|---|
| 1 | Separation of reasoning vs. enforcement | `Gate` runs zero LLM calls at runtime; agent supplies inputs, gate judges | No probabilistic logic on the enforcement path |
| 2 | Mandatory context lineage | `EvidenceManifest` schema; gate rejects any action lacking one | Every action is traceable to sourced facts |
| 3 | Deterministic policy evaluation | `PolicyEngine.evaluate(action, manifest, policy)` — a pure function | Same inputs → same decision, byte-for-byte |
| 4 | Fail-safe & human-in-the-loop | `Decision` enum + `ReviewQueue` routing | Never fails silently; blocks or routes without breaking the agent loop |
| 5 | Telemetry & observability | Append-only, hash-chained `AuditLog` | Full, tamper-evident record of every gate interaction |

---

## 3. Architecture

```
                    ┌────────────────────────────────────────────┐
   AGENT RUNTIME    │  reasoning · tool selection · payload build  │
   (probabilistic)  │  + declares Evidence Manifest                │
                    └───────────────────┬──────────────────────────┘
                                        │  ProposedAction + EvidenceManifest
                        ══════════ ENFORCEMENT BOUNDARY ══════════
                                        ▼
                    ┌────────────────────────────────────────────┐
   EVIDENCE GATE    │ 1. Structural validation (manifest present  │
   (deterministic)  │    & well-formed) ──► else BLOCK             │
                    │ 2. PolicyEngine.evaluate(...) ──► Decision   │
                    │ 3. AuditLog.append(signed record)           │
                    │ 4. Route: ALLOW→execute · REVIEW→queue ·     │
                    │    RESTRICT→execute-degraded · BLOCK→stop    │
                    └───────────────────┬──────────────────────────┘
                    ALLOW / RESTRICT    │    REVIEW / BLOCK
                          ▼             │           ▼
                 ┌─────────────────┐    │   ┌──────────────────┐
                 │  Real tool call │    │   │   ReviewQueue    │
                 │ (refund, email) │    │   │  (human / eval)  │
                 └─────────────────┘    │   └──────────────────┘
                                        ▼
                              ┌──────────────────┐
                              │  AuditLog (chain) │
                              └──────────────────┘
```

The gate is an **in-process library** the agent calls before executing a
sensitive tool. Logically it is a hard boundary: the agent never touches the
downstream tool directly on a gated path — it goes through `gate.check(...)` and
executes only on `ALLOW`/`RESTRICT`. (Section 9 notes how the same boundary lifts
into a standalone service later.)

### 3.1 Why in-process, and why it's still "decoupled"

Requirement #1 demands the enforcement layer be *decoupled from the LLM runtime*.
"Decoupled" is about **logic**, not process boundaries: the gate contains no model
calls, no prompts, no sampling — it is ordinary deterministic code. Running it
in-process keeps the demo simple and latency near-zero; the `check()` interface is
deliberately a pure request/response so it can be moved behind HTTP unchanged.

---

## 4. Data model

All schemas are Pydantic models (typed, validated, JSON-serializable).

### 4.1 `EvidenceItem` — one fact the agent relied on

```python
class EvidenceSource(str, Enum):
    TOOL_RESULT = "tool_result"      # observed from a tool/API call
    RETRIEVAL   = "retrieval"        # RAG / document store
    USER_INPUT  = "user_input"       # provided in the conversation
    MEMORY      = "memory"           # agent long-term memory / profile field
    INFERENCE   = "inference"        # derived/computed by the agent (not observed)

class EvidenceItem(BaseModel):
    id: str                          # stable id, referenced by rules
    claim: str                       # human-readable fact ("opt_in = true")
    key: str                         # machine key the rules match on, e.g. "marketing.opt_in"
    value: Any                       # the fact's value
    source: EvidenceSource
    source_id: str                   # provenance: doc id, tool call id, msg id
    observed_at: datetime            # when the underlying fact was true/fetched
    confidence: float = 1.0          # 0..1 signal from the agent
    observed: bool                   # True = directly observed, False = inferred
```

### 4.2 `EvidenceManifest` — the full lineage for one action

```python
class EvidenceManifest(BaseModel):
    items: list[EvidenceItem]
    # conflicts the agent itself detected; the engine also detects them independently
    declared_conflicts: list[tuple[str, str]] = []
    compiled_at: datetime

    def by_key(self, key: str) -> list[EvidenceItem]: ...
```

### 4.3 `ProposedAction` — what the agent wants to do

```python
class ProposedAction(BaseModel):
    action: str                      # canonical id, e.g. "marketing.send_sequence"
    payload: dict                    # the assembled tool arguments
    actor: str                       # agent / principal id (RBAC still applies upstream)
    request_id: str                  # idempotency + audit correlation
```

### 4.4 `Decision` — the gate's verdict

```python
class Effect(str, Enum):
    ALLOW    = "allow"       # execute as-is
    RESTRICT = "restrict"    # execute a degraded/limited variant
    REVIEW   = "review"      # route to human/eval; do not execute yet
    BLOCK    = "block"       # refuse

class RuleResult(BaseModel):
    rule_id: str
    effect: Effect
    reason: str
    evidence_refs: list[str]         # which EvidenceItem ids drove this

class Decision(BaseModel):
    effect: Effect                   # aggregate (most restrictive wins)
    results: list[RuleResult]        # every rule that fired, for explainability
    request_id: str
    decided_at: datetime
```

---

## 5. Policy model (own typed rule schema)

Rules are **declarative data**, authored in YAML, loaded into typed models, and
evaluated by a hand-written engine. No embedded expression language, no `eval`,
no external policy runtime — every operator is a named, tested primitive. This is
the strongest possible determinism/auditability story and has zero dependencies.

### 5.1 Shape

A `Policy` is a versioned `RulePack`: a list of rules keyed by the action(s) they
govern. Each rule is a set of **requirements** over the manifest; if the
requirements are not satisfied, the rule yields an `effect` and `reason`.

```yaml
# policies/marketing.yaml
version: "2026-07-05.1"
action: "marketing.send_sequence"
rules:
  - id: opt_in_required
    description: "Marketing requires a verified, fresh opt-in."
    requirements:
      - key: "marketing.opt_in"
        must_exist: true              # missing → effect_on_fail
        equals: true
        source_in: [tool_result, retrieval]   # unauthorized source → fail
        observed: true                # must be observed, not inferred
        max_age: { months: 12 }       # stale → effect_on_fail
        min_confidence: 0.9
    effect_on_fail: block             # missing/unauthorized/wrong value
    effect_on_stale: review           # exists but violates max_age
  - id: no_conflicts
    description: "No unresolved conflicting evidence for opt-in."
    forbid_conflicts_on: ["marketing.opt_in"]
    effect_on_fail: review
```

### 5.2 Requirement primitives (the complete operator set)

Each is a pure, individually-tested predicate over the evidence for a `key`:

| Primitive | Meaning | Failure maps to |
|---|---|---|
| `must_exist` | at least one evidence item for `key` | **missing** → `effect_on_fail` |
| `equals` / `in` | value constraint | wrong value → `effect_on_fail` |
| `source_in` | allowed provenance sources | **unauthorized** → `effect_on_fail` |
| `observed` | must be directly observed, not inferred | **unauthorized/weak** → `effect_on_fail` |
| `max_age` | `now - observed_at` within window | **stale** → `effect_on_stale` |
| `min_confidence` | confidence floor | weak → `effect_on_fail` |
| `forbid_conflicts_on` | no two items on `key` disagree | **conflicting** → `effect_on_fail` |

This directly encodes the four failure modes from the spec:
**missing → BLOCK**, **stale → REVIEW**, **conflicting → REVIEW**,
**unauthorized → BLOCK** (defaults; each is a per-rule knob).

### 5.3 Conflict detection

The engine independently groups evidence by `key` and flags a conflict when two
`observed` items carry unequal `value`s (after normalization). It does not trust
the agent's `declared_conflicts` to be complete — the agent declaring a conflict
is corroborating signal, not the source of truth. Undeclared conflicts the engine
finds are themselves an audit-worthy event.

### 5.4 Aggregation — most-restrictive-wins

`evaluate()` runs every rule for the action and combines results with a fixed
lattice: `BLOCK > REVIEW > RESTRICT > ALLOW`. If no rule governs the action, the
default is `BLOCK` (**deny-by-default** — an ungoverned sensitive action is a
policy gap, not an implicit allow). This default is explicit and configurable per
deployment but ships closed.

### 5.5 Determinism contract

`evaluate(action, manifest, policy)` is a pure function of its inputs plus an
explicitly-injected `now` timestamp. No wall-clock reads inside the engine, no
randomness, no I/O, no network. Property: **identical `(action, manifest, policy,
now)` ⇒ identical `Decision`**, enforced by golden tests. `now` is passed in (not
read from the clock) precisely so the "stale" branch is reproducible in tests and
in audit replay.

---

## 6. The Gate (SDK surface)

```python
gate = Gate(policy_set=PolicySet.from_dir("policies/"),
            audit=AuditLog("audit.log"),
            review=ReviewQueue())

decision = gate.check(action, manifest, now=clock.now())

match decision.effect:
    case Effect.ALLOW:     result = tool.execute(action.payload)
    case Effect.RESTRICT:  result = tool.execute(degrade(action.payload))
    case Effect.REVIEW:    review_ticket = decision  # already queued by the gate
    case Effect.BLOCK:     raise ActionBlocked(decision)
```

Ergonomic wrapper for the common case (decorator around a tool function):

```python
@gate.enforce(action="marketing.send_sequence")
def send_sequence(payload, *, manifest): ...
# raises ActionBlocked on BLOCK, returns a ReviewPending sentinel on REVIEW,
# executes on ALLOW/RESTRICT — the agent loop keeps running either way.
```

`gate.check` responsibilities, in order:

1. **Structural validation** — manifest present and schema-valid, else synthesize
   a `BLOCK` decision with reason `manifest_missing` (req #2 is enforced here,
   before any policy runs).
2. **Evaluate** via `PolicyEngine`.
3. **Audit** — append a signed record (§7) *before returning*, so nothing
   executes unrecorded.
4. **Route** — on `REVIEW`, enqueue the full assembled context (action + manifest
   + decision) to `ReviewQueue` and return without raising, so the agent's loop is
   not broken (req #4).

---

## 7. Telemetry & audit (req #5)

Every `check()` appends one record capturing **the proposed payload, the provided
evidence, the specific rule(s) triggered, and the final routing decision**:

```python
class AuditRecord(BaseModel):
    seq: int
    request_id: str
    action: ProposedAction
    manifest: EvidenceManifest
    decision: Decision
    policy_version: str
    approver: str | None             # set when a human resolves a REVIEW
    prev_hash: str                   # hash-chain link
    hash: str                        # sha256(prev_hash + canonical(this record))
```

- **Append-only + hash-chained** → tamper-evident ("signed audit trail"). Any
  edit to a past record breaks the chain at that point.
- **Canonical serialization** (sorted keys, normalized timestamps) so hashes are
  reproducible and the log can be replayed to re-derive decisions.
- Records are the substrate for the eval loop: filter to `REVIEW`/`BLOCK` to mine
  edge cases and turn recurring failures into new rules (the "trace-to-gate" loop).

---

## 8. Human-in-the-loop routing (req #4)

`ReviewQueue` is an interface with a stub in-memory implementation for the demo:

```python
class ReviewQueue(Protocol):
    def enqueue(self, decision: Decision, action: ProposedAction,
                manifest: EvidenceManifest) -> str: ...   # returns ticket id
    def resolve(self, ticket_id: str, approver: str,
                effect: Effect) -> Decision: ...           # human override, re-audited
```

Key property: routing to review **does not block or crash the agent loop**. The
gate returns a `REVIEW` decision; the agent treats the action as pending and can
proceed with other work. When a human (or a separate eval agent) resolves the
ticket, the resolution is itself audited with the `approver` set. The assembled
context is never dropped — it lives in the queue and the audit log.

---

## 9. What's in scope vs. later

**In scope for the prototype (once we build):**
schemas · policy engine + YAML loader · gate + decorator · hash-chained audit ·
in-memory review queue · a demo agent exercising the marketing + refund scenarios
· golden/property tests for determinism and each failure mode.

**Deliberately deferred:**
- **Trace ingestion → candidate rules** (distinct from replay: mining logs to *propose* policy, not just simulate it).
- **Asymmetric signing** — swap HMAC for public-key so verifiers need no shared secret; a drop-in over the current `Signer` (DESIGN §13.5).
- **RBAC/ABAC** — assumed upstream; the gate is orthogonal and additive.

*(Note: The HTTP gate service, cryptographic signing with HMAC keys, the offline policy compiler, and SQLite persistent review queues are fully realized as of v0.4.1 — see subsequent sections.)*

---

## 10. Worked example — the marketing tripwire

Rule: *"A marketing workflow cannot be triggered unless the user's opt-in
timestamp is verified and less than 12 months old."*

| Scenario | Manifest for `marketing.opt_in` | Decision | Why |
|---|---|---|---|
| Happy path | observed, `tool_result`, `observed_at` 2 months ago, conf 1.0 | **ALLOW** | all requirements pass |
| Hallucinated request | *no item for the key* | **BLOCK** | `must_exist` fails → missing |
| Stale opt-in | observed, 14 months ago | **REVIEW** | `max_age` fails → `effect_on_stale` |
| Inferred, not observed | `source=inference`, `observed=false` | **BLOCK** | `observed`/`source_in` fail → unauthorized |
| Conflicting sources | two observed items: `true` and `false` | **REVIEW** | conflict on key |

Same five inputs, replayed against the same policy version, always produce the
same five decisions — and every one leaves a chained audit record.

---

## 11. Proposed module layout

Project managed with **`uv`** — `uv init` for the project, `uv add pydantic
pyyaml` for deps, `uv add --dev pytest`, `uv run pytest` / `uv run
examples/demo_agent.py` to execute. A `pyproject.toml` + `uv.lock` pin everything
for reproducible runs (which matters for a system whose whole value is
determinism).

Managed with **`uv`**. The core (`schemas` · `policy` · `engine` · `gate` ·
`audit` · `review` · `trace` · `trace_adapters` · `signing` · `telemetry` · `errors`) depends
only on `pydantic` + `pyyaml`; everything network- or framework-facing is an
optional extra, so the base install never pulls FastAPI / httpx / LangChain /
CrewAI / LlamaIndex / OpenTelemetry. As-built (v0.4.1):

```
pyproject.toml      # uv-managed; base deps: pydantic, pyyaml; extras below
uv.lock
src/evidence_gate/
  schemas.py        # EvidenceItem, EvidenceManifest, ProposedAction, Decision
  policy.py         # typed rule models (incl. Comparison) + YAML loader
  engine.py         # PolicyEngine.evaluate() — pure, deterministic
  gate.py           # Gate.check() + @enforce decorator
  audit.py          # hash-chained append-only AuditLog (optional JSONL sink)
  review.py         # ReviewQueue protocol + in-memory & SQLite impls
  errors.py         # custom exceptions (ClearanceDenied, GateUnreachable, etc.)
  trace.py          # ManifestBuilder — derive a manifest from tool-call traces
  trace_adapters.py # normalize() + simulate() + coverage() + vendor presets
  signing.py        # HMAC Signer/Verifier + require_clearance downstream guard
  telemetry.py      # DecisionEvent + OTelSink — payload-safe span events [otel]
  service.py        # create_app() — FastAPI gate service              [service]
  client.py         # RemoteGate — fail-closed remote client           [client]
  cli.py            # evidence-gate: replay + audit verify   [console entrypoint]
  integrations/
    base.py         # GatePort / GateSession seam shared by all adapters
    langchain.py    # EvidenceGateCallbackHandler                    [langchain]
    crewai.py       # gate_crew_tools() — gated BaseTool wrappers        [crewai]
    llamaindex.py   # gate_llama_tools() — gated FunctionTool wrappers [llamaindex]
policies/
  marketing.yaml
  refund.yaml
examples/           # demo_agent, refund_agent (RESTRICT), llm_agent (real LLM),
                    # remote_agent, trace_replay, langchain_agent
tests/              # golden + property + integration tests (159)
```

Extras: `service` (fastapi, uvicorn) · `client` (httpx) · `langchain`
(langchain-core) · `crewai` (crewai) · `llamaindex` (llama-index-core) · `otel`
(opentelemetry-api).

---

## 12. Resolved decisions

1. **Manifest ownership — both.** The agent may supply an `EvidenceManifest`
   directly, **and** the gate can build/augment one from a trace. Both paths
   converge on the same validated schema before the engine runs. v1 ships the
   agent-supplied path first; a `ManifestBuilder` seam is left for the
   trace-derived path.
2. **`RESTRICT` — keep.** First-class effect; ALLOW/RESTRICT execute
   (RESTRICT via a degraded payload), REVIEW/BLOCK do not.
3. **Ungoverned action — deny-by-default (`BLOCK`).** An action with no governing
   rule is a policy gap, not an implicit allow. Explicit and configurable, ships
   closed.
4. **Cross-key rules — deferred, seam left open.** The fixed primitive set (§5.2)
   covers the spec's examples. Cross-key comparisons (e.g. "refund ≤ order total")
   are not in v1; the requirement model is structured so a `compare_keys`
   primitive can be added without reworking the engine.

---

## 13. First shippable version (v0.2.0 alpha)

The prototype (§9) proved the thesis on the marketing tripwire. v0.2.0 is the
first version we consider **shippable**: every leg of the thesis is exercised by
a runnable example and pinned by a test, with no stub on the critical path. Three
decisions from §12 that were "seam left open" are now realized.

### 13.1 What shipped

| Capability | Where | Realizes |
|---|---|---|
| Cross-key `compare` primitive | `policy.py:Comparison`, `engine.py:_check_comparison` | §12.4 (`compare_keys`) |
| `RESTRICT` execution path (real degradation) | `policies/refund.yaml`, `examples/refund_agent.py` | §12.2 |
| Trace-derived manifests | `trace.py:ManifestBuilder` / `ToolCall` | §12.1 (second manifest path) |
| Live LLM behind the gate | `examples/llm_agent.py` | §1 thesis, end-to-end |

### 13.2 The `compare` primitive

A rule may carry one `Comparison` relating the representative value of one
evidence key to another key or an authored literal:

```yaml
- id: refund_within_order_total
  compare: { left_key: "refund.amount", op: "<=", right_key: "order.total" }
  effect_on_fail: block            # can never refund more than was charged
- id: refund_under_auto_ceiling
  compare: { left_key: "refund.amount", op: "<=", right_value: 5000 }
  effect_on_fail: restrict         # over the ceiling -> capped partial, not blocked
```

Design properties, consistent with §5:

- **Named operators, no expression language.** `op` is one of
  `< <= > >= == !=`, each dispatched to a Python `operator` function. No `eval`,
  no parser.
- **Numeric guard.** Relational ops (`< <= > >=`) require numeric operands;
  `bool` is explicitly *not* a number, so `True` can never sort as `1`. Equality
  ops (`== !=`) work on any value.
- **Representative value.** For each key the comparison uses the newest
  *observed* item (`engine._representative`), so a stale or inferred duplicate
  can't quietly set the compared value. This composes with the per-key
  requirements that already vet provenance and freshness.
- **Unprovable ⇒ fail.** A missing left or right operand fails the rule (routed
  by `effect_on_fail`) rather than silently passing — an absent value is not
  evidence of compliance.

`Comparison` validates that exactly one of `right_key` / `right_value` is set.
The engine evaluates `compare` *after* the rule's `requirements`, so operand
provenance is vetted before the arithmetic.

### 13.3 The `RESTRICT` path, made concrete

`RESTRICT` was always a first-class effect (§12.2) but had no executing example.
The refund policy now yields it: a refund over the auto-approve ceiling is
degraded rather than refused. The `@gate.enforce` decorator passes the decided
`effect` into the tool, which degrades its own payload:

```python
@gate.enforce(action="billing.issue_refund")
def issue_refund(payload, effect):
    amount = payload["refund_amount"]
    if effect is Effect.RESTRICT:
        amount = min(amount, 5000)     # execute a capped partial
    return {"refunded": amount, "mode": effect.value}
```

Same manifest, three verdicts, all recorded: `4000 → ALLOW (4000)`,
`6500 → RESTRICT (5000)`, `9000 → BLOCK` (exceeds order total).

### 13.4 Trace-derived manifests

§12.1 resolved manifest ownership as "both": agent-supplied *and* gate-built.
v0.1 shipped only the agent-supplied path. `ManifestBuilder` adds the second:
register a deterministic `Extractor` per tool, and `build(calls, compiled_at)`
maps a recorded `ToolCall` trace into an `EvidenceManifest`. It adds **no**
evaluation logic — both paths converge on the same validated schema before the
engine runs. A tool with no registered extractor contributes nothing; evidence
is opt-in, never inferred. A `ToolCall` is the minimal normalized shape a
LangSmith / Langfuse / OpenAI-log adapter would target — that adapter is the next
increment (§9), and it feeds this seam without touching the engine.

### 13.5 Ship boundary

Intentionally **not** in v0.2.0, each with its seam already open so it stays
additive rather than a rewrite:

- **HTTP gate service** — `check()` is a pure request/response; FastAPI is a wrapper.
- **Real-key signing** — the hash chain's `_hash` swaps for HMAC/asymmetric in place.
- **Trace-log adapter** — normalizes vendor logs into the `ToolCall` shape above.
- **Offline policy compiler** — an authoring convenience; the runtime never needs it.

### 13.6 Verification bar

The version is "shippable" because the whole surface is pinned: **45 golden +
property tests** cover the four failure modes, most-restrictive aggregation,
deny-by-default, the `compare` operand/numeric-guard cases, RESTRICT degradation
through the decorator, trace-derived manifests driving real verdicts, and
hash-chain tamper/reorder detection. Determinism is asserted directly —
identical `(action, manifest, policy, now)` yields a byte-identical `Decision`.

---

## 14. Integration surface & signing (v0.3.0 alpha)

v0.3.0 makes the core gate adoptable, adding support for distributed and secure agent architectures.

### 14.1 Standalone HTTP Gate Service
A FastAPI application serves the gate over HTTP (`service.py`). It exposes endpoints:
- `/v1/check`: Receives a `ProposedAction` and `EvidenceManifest` (or builds a manifest from tool calls), executes the core engine, audits the verdict, and returns a JSON `Decision`.
- `/v1/review` & `/v1/audit`: Provide JSON access to review tickets and the hash-chained audit log.

### 14.2 Fail-Closed Remote Client
The `RemoteGate` (`client.py`) targets the HTTP gate service with strict **fail-closed** rules:
- Any network failure or non-200 response from the service raises `GateUnreachable`, preventing tool execution.
- Ergonomic pattern mapping (`auto_instrument`) wraps tool methods based on name globs (e.g. `stripe_*` -> `billing.issue_refund`), intercepting tool calls transparently at the boundary.

### 14.3 Cryptographic Signing
To prevent clients from forging approval verdicts in distributed architectures, the gate issues cryptographically secure, HMAC-signed **clearance tokens** (`signing.py`):
- For `ALLOW` and `RESTRICT` verdicts, the gate service signs a payload containing the `request_id`, `action`, and `effect` using a shared HMAC key (`Signer`).
- The client validates this token (`Verifier`) before executing the target tool.
- The `Signer` and `Verifier` also sign the audit log hash chain to ensure historical record integrity.

### 14.4 Trace-to-Gate Replay
The `normalize()` and `simulate()` routines (`trace_adapters.py`) allow historical tool-call logs (traces) to be mapped and replayed through a local or remote gate. This lets teams diagnose policy applicability on past agent trajectories before turning on active blocking.

---

## 15. Multi-framework adapters & telemetry (v0.4.0 alpha)

v0.4.0 extends integration capability across agent frameworks and integrates clean, production-grade telemetry.

### 15.1 The `GatePort` & `GateSession` Seams
To keep the framework adapters decoupled from the communication protocol (in-process vs. HTTP), v0.4.0 introduces the `GatePort` interface (`integrations/base.py`). 
A `GateSession` manages the state of one agent execution turn, acting as an in-memory compiler that aggregates evidence items generated by tools and queries the `GatePort` when sensitive tools are invoked.

### 15.2 Agent Framework Adapters
- **LangChain Callback**: `EvidenceGateCallbackHandler` collects tool outputs and enforces checks directly during LangChain agent executor runs.
- **CrewAI & LlamaIndex**: Since these frameworks' event pipelines are non-blocking, the adapters (`gate_crew_tools` and `gate_llama_tools`) wrap the tool functions themselves, referencing a shared `GateSession` to assert clearance before the inner function runs.

### 15.3 Payload-Safe Decision Telemetry
The `OTelSink` (`telemetry.py`) records decisions to OpenTelemetry spans as span events (`evidence_gate.decision` or `evidence_gate.pending_review`). 
To ensure strict privacy and avoid leaking sensitive logs, the telemetry event is designed to be payload-safe: it includes only non-sensitive scalars (e.g. action id, version, verdict, rule IDs, and evidence count) and explicitly excludes raw tool arguments, prompt contents, LLM statements, and the content values of evidence claims.

---

## 16. Adoption CLI, downstream enforcement, SQLite review, and compiler (v0.4.1 alpha)

v0.4.1 provides command-line tools for onboarding, extends enforcement into backend services, and adds persistent enterprise queuing.

### 16.1 The `evidence-gate` CLI
A command-line interface (`cli.py`) wraps the onboarding and operational flows:
- `evidence-gate replay <trace>`: Replays recorded JSON/JSONL runs and outputs step-by-step verdicts.
- `evidence-gate audit verify <log>`: Verifies the integrity of a hash-chained log.
- `evidence-gate policy lint <file>`: Statically validates a YAML policy file against the schema and flags warnings.
- `evidence-gate policy compile <sop>`: Translates standard operating procedures into policy files.

### 16.2 Residual-Risk Coverage Report
Replay outputs a coverage breakdown separating tools into three groups:
1. **Gated**: Sensitive actions wrapped by an enforce rule.
2. **Recognized Evidence**: Informational tools whose outputs feed extractors.
3. **Unclassified**: Raw tools executed in the trace that are neither gated nor recognized as evidence. This directly highlights security gaps (unwrapped sensitive tools) that may lead to untyped residual risk.

### 16.3 Downstream Token Enforcement
To ensure that authorization cannot be bypassed by directly invoking the backend execution tools, the `@require_clearance` decorator (`signing.py`) guards backend operations. It intercepts execution, verifying that a valid, unexpired, action-bound HMAC clearance token was supplied by the caller.

### 16.4 SQLite Persistent Review Queue
The `SQLiteReviewQueue` (`review.py`) replaces the prototype's volatile in-memory queue for multi-process or production environments:
- Stores tickets in a SQLite table with a dedicated index on the `resolved` column for fast retrieval of pending reviews.
- Uses Write-Ahead Logging (`journal_mode = WAL`) and a database busy timeout to safely support concurrent writes across multiple process/thread workers.
- Generates monotonic, collision-free review ticket IDs directly via database sequence autoincrements.

### 16.5 Offline Policy Compiler
The policy compiler (`compiler.py`) translates natural language standard operating procedures (SOPs) into candidate policy YAML files using a generative LLM drafter:
- **Offline Only**: The compiler module is kept strictly separate from the gate's runtime decision loop.
- **Schema & Semantic Linter**: Automatically lints generated YAML drafts against the Pydantic schema and flags semantic warnings (e.g., rules that fail-open or lack stale routing).
- **Mandatory Approval Workflow**: A draft is inert and cannot be activated to the policies folder unless an explicit `approve(approver)` invocation stamps it with a human operator's identity.
```
