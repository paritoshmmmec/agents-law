# Roadmap

**Core — working since v0.2.0**

- [x] Typed schemas — `EvidenceItem` / `EvidenceManifest` / `ProposedAction` / `Decision`
- [x] Deterministic policy engine — pure `evaluate(action, manifest, policy, now)`
- [x] The seven requirement primitives + the four failure modes (`missing` / `stale` / `conflicting` / `unauthorized`)
- [x] `Gate.check()` + `@enforce` decorator; deny-by-default for ungoverned actions
- [x] Hash-chained, tamper-evident audit log; audited human-review resolution
- [x] In-memory review queue that never breaks the agent loop
- [x] Real LLM agent (`examples/llm_agent.py`) driving the gate end-to-end
- [x] **`RESTRICT` execution path** — large refund → capped partial (`examples/refund_agent.py`)
- [x] **Cross-key `compare` rules** — `refund.amount ≤ order.total`
- [x] **Trace-derived manifests** — `ManifestBuilder` from recorded tool calls

**Integration surface — new in v0.3.0**

- [x] **HTTP gate service** — `create_app()`, a FastAPI wrapper over `gate.check()` with both manifest paths
- [x] **Fail-closed remote client** — `RemoteGate` with name-pattern `auto_instrument`, `ClearanceDenied` / `GateUnreachable`
- [x] **Real-key signing** — HMAC `Signer`/`Verifier`; signed clearance tokens on ALLOW/RESTRICT; backwards-compatible audit-chain hash
- [x] **Trace-to-Gate replay** — generic `normalize()` + `simulate()` over the `ManifestBuilder` seam
- [x] **LangChain adapter** — `EvidenceGateCallbackHandler` over a local/remote `GatePort` seam

**Adapters, presets & telemetry — new in v0.4.0**

- [x] **Per-vendor trace presets** — `LANGSMITH` / `LANGFUSE` / `OPENAI` `TraceMapping`s over the generic seam
- [x] **CrewAI + LlamaIndex adapters** — `gate_crew_tools` / `gate_llama_tools` over the shared `GatePort` / `GateSession` seam
- [x] **OTel span events** — `evidence_gate.decision` / `.pending_review` via `OTelSink`, excluding raw args/prompt/model output

**Adoption CLI, coverage & downstream enforcement — new in v0.4.1**

- [x] **CLI entrypoint** — `evidence-gate replay <trace> …` and `evidence-gate audit verify <log>` over the existing `simulate`/`AuditLog` seams
- [x] **Residual-risk / coverage report** — `coverage()` classifies every tool in a trace as gated / recognized-evidence / **unclassified**; folded into `replay` output
- [x] **Downstream token enforcement** — `require_clearance(verifier, action=…)` refuses any downstream call lacking a fresh, action-bound clearance token

**Pending for the alpha line (next)**

- [ ] **Persistent review backend** — the queue is in-memory; add a durable store for multi-process deployments
- [ ] **Key rotation / revocation** — support a `kid` header so multiple `Signer` keys validate during rotation; document rotate + revoke

**Deliberately deferred** (see `DESIGN.md` §9)

- [ ] **Offline policy compiler** (SOP text → reviewed rule pack via an LLM) + approve/version workflow
- [ ] **Trace ingestion → candidate rules** (mining logs to *propose* policy, not just simulate it)
- [ ] **Asymmetric signing** — swap HMAC for public-key so verifiers need no shared secret
- [ ] **RBAC/ABAC** — assumed upstream; the gate is orthogonal and additive

## Release notes

### v0.4.1 alpha — adoption CLI, coverage & downstream enforcement

Three additions that make the *simulate → enforce* funnel walk-able end-to-end
from a shell, and make the clearance token actually load-bearing — all still thin
plumbing over the pure `check()` / `simulate()` / `Signer` seams:

- **Adoption CLI.** `evidence-gate replay` runs a recorded trace through the gate
  and prints the per-call verdicts; `evidence-gate audit verify` recomputes a JSONL
  audit chain and exits non-zero if it was tampered.
- **Residual-risk coverage.** `coverage()` classifies every tool in a trace as
  gated, recognized-evidence, or **unclassified** — the last being a route no
  policy or extractor accounts for. Surfaced by name in the `replay` output.
- **Downstream token enforcement.** `require_clearance(verifier, action=…)` guards
  a downstream effect so it refuses any call without a fresh, action-bound
  clearance token.

Verified: 127 tests pass; the base package still imports with no extra; `replay`
and `audit verify` run end-to-end; the guard fails closed on missing / forged /
expired / wrong-action tokens.

### v0.4.0 alpha — adapters, presets & telemetry

- **Per-vendor trace presets.** `LANGSMITH` / `LANGFUSE` / `OPENAI` ready-made
  `TraceMapping`s for each vendor's per-observation shape.
- **Two more framework adapters.** `gate_crew_tools` (CrewAI) and `gate_llama_tools`
  (LlamaIndex) join the LangChain handler over a shared `GatePort` / `GateSession` seam.
- **Payload-safe decision telemetry.** `OTelSink` adds a decision event carrying only
  the verdict, rules fired, and evidence keys/counts — never args, prompts, model
  output, or evidence values.

Verified: 110 tests pass; the hygiene boundary is tested independently of OTel.

### v0.3.0 alpha — the integration surface

- **Remote, fail-closed enforcement.** `create_app()` serves the gate over HTTP;
  `RemoteGate` refuses to execute when the gate is unreachable.
- **Zero-touch instrumentation.** `RemoteGate.auto_instrument(...)` wraps existing
  tools by name pattern; call sites don't change.
- **Signed clearance.** HMAC `Signer`/`Verifier` issues short-lived tokens on
  ALLOW/RESTRICT and keeps the audit-chain hash byte-identical with `key=None`.
- **Trace-to-Gate onboarding.** `normalize()` + `simulate()` replay a recorded trace.
- **Framework adapter.** `EvidenceGateCallbackHandler` gates a LangChain agent's
  sensitive tool call.

Verified: 89 tests pass; the audit chain is regression-clean; all three new examples
run end-to-end.

### v0.2.0 alpha — the core

The first version coherent enough to hand to someone else and have them gate a real
action end-to-end:

- **A real agent can drive it** (`examples/llm_agent.py`).
- **All four verdicts execute**, including `RESTRICT` (`examples/refund_agent.py`).
- **Rules can span keys** via the `compare` primitive.
- **Manifests can come from execution** via `ManifestBuilder`.
- **Everything is recorded and reproducible** — hash-chained audit + pure engine;
  45 golden + property tests.
