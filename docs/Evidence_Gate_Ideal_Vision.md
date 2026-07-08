# Evidence Gate — Refined Vision, PRD & North Star

*(Working name — see Section 6 for naming recommendations)*

---

## 1. Refined Product Vision

Evidence Gate is the **evidence firewall for agentic software**: a small, deterministic layer that sits between an AI agent and any consequential action, and refuses to let that action proceed unless the agent can show fresh, authorized, non‑conflicting evidence that it's justified.

The core idea, sharpened:

> **RBAC answers "is this agent allowed to use this tool, ever?" Evidence Gate answers "is this specific call, right now, backed by facts the agent was actually entitled to rely on?"**

That distinction is the whole product. Everything else — replay, policy authoring, clearance tokens, audit trails — exists to make that one question cheap to ask and impossible to fake.

**The adoption path is the product.** Teams don't need to trust Evidence Gate before they can use it:

1. Point it at yesterday's traces → see what would have been allowed, restricted, sent to review, or blocked, with reasons. Zero production risk.
2. Wrap one sensitive tool by name pattern → move that single tool from simulated to live, fail‑closed enforcement.
3. Expand tool-by-tool, with every decision hash-chained and every allowed action carrying a short-lived, verifiable clearance token.

No rewrite, no framework migration, no "trust the black box" leap of faith.

**What changed from the original draft:**
- Made the RBAC-vs-Evidence-Gate distinction the lead, not a footnote — it's the sharpest, most defensible piece of positioning here and should do more work.
- Reframed "ideal experience" around *zero-trust adoption* (simulate before you enforce) as the core wedge, since that's what makes this adoptable inside orgs that won't let an unproven system near production.
- Tightened "audit-native" to be explicit that audit records must be safe to hand to a third party (no raw prompts/PII) by default, not as an afterthought.

---

## 2. North Star

**North Star Statement** (kept from original, it's strong):

> Can we prove why this agent took this action, using facts it was actually allowed to rely on?

If the honest answer is no, the action is blocked, restricted, or routed to a human — **before** it executes, not after.

**North Star Metric:**

> **% of consequential agent actions that execute with a valid, time-fresh, non-conflicting evidence chain attached — target 100% for wrapped tools.**

**Supporting metrics:**
| Metric | Why it matters |
|---|---|
| Time-to-block for unjustified actions | Target: 0 (pre-execution, not detected after the fact) |
| False-restriction rate | Guards against the system becoming security theater that teams route around |
| Time from "connect traces" to first diagnostic report | Proxy for adoption friction — should be minutes, not days |
| % of allowed actions with a verifiable clearance token consumed downstream | Proxy for whether enforcement is actually load-bearing, not decorative |

**Anti-metric to watch:** latency added per tool call. A deterministic gate that's slow enough to get bypassed defeats its own purpose.

---

## 3. Positioning

Evidence Gate is **not**:
- an agent framework (LangChain, CrewAI, etc. sit upstream of it)
- an observability/tracing tool (it consumes traces, doesn't replace them)
- an RBAC/IAM replacement (it assumes authz already happened)
- an LLM-based judge (enforcement is deterministic, compiled policy — never a model's opinion at runtime)

It **is** the deterministic control point that existing tools miss:

> The agent is authorized. Is this action actually justified by the evidence?

---

## 4. Target Users / Personas

- **Platform/infra engineer** building an internal agent platform who needs a control point that doesn't require rewriting every tool integration.
- **Security or compliance lead** who needs to answer "why did the agent do that" for an auditor, not just "what did it do."
- **AI product engineer** shipping an agent that touches money, infra, or customer data and wants a fail-closed guardrail without building one from scratch.
- **Auditor/compliance reviewer** who needs tamper-evident, replayable decision logs without access to raw prompts.

---

## 5. Product Requirements

### 5.1 Problem Statement
Agent frameworks make it easy to grant an agent tool access. Almost nothing checks, at the moment of the call, whether the specific action is backed by evidence that's fresh, authorized, and internally consistent. Today that gap is closed (if at all) by ad hoc prompt instructions or post-hoc log review — neither of which prevents the bad action from happening.

### 5.2 Goals / Non-Goals

**Goals**
- Let a team simulate enforcement against historical traces with no code changes.
- Let a team move a single named tool to live enforcement in under a day.
- Guarantee runtime decisions are deterministic and reproducible from the same policy + evidence inputs.
- Make every decision (allow/restrict/review/block) explainable and hash-chained.
- Support signed, short-lived clearance tokens that downstream services can verify independently.

**Non-goals**
- Replacing agent orchestration frameworks.
- Making authorization decisions (who *can* call a tool) — that's RBAC's job.
- Runtime use of an LLM as the enforcement decision-maker.
- General-purpose observability/tracing (Evidence Gate is a consumer of traces, not a competitor to LangSmith/Langfuse).

### 5.3 Core Product Flow

```
 Traces (LangSmith / OpenAI / Langfuse / custom)
        │
        ▼
   [1] REPLAY  ──────────► import historical traces, no live risk
        │
        ▼
   [2] DIAGNOSE ─────────► allow / restrict / review / block, per action, with reasons
        │
        ▼
   [3] AUTHOR ───────────► write/generate readable YAML policy from SOPs → human approves
        │
        ▼
   [4] ENFORCE ──────────► wrap sensitive tool(s) by name pattern or framework adapter
        │                  (fail-closed at runtime)
        ▼
   [5] VERIFY ───────────► downstream services check signed clearance token
        │
        ▼
   [6] AUDIT ────────────► hash-chained, replayable, exportable decision log
```

### 5.4 Functional Requirements by Phase

**Phase 1 — Simulate (MVP)**
- Trace ingestion adapters: LangSmith, OpenAI, Langfuse, generic JSON schema.
- Policy compiler: YAML → deterministic rule set.
- Diagnostic engine: classify each historical action as allow/restrict/review/block with a human-readable reason.
- Reporting surface (CLI + minimal dashboard) summarizing coverage and findings.

**Phase 2 — Enforce (V1)**
- Tool-wrapping SDK: decorator/middleware matching on tool name pattern.
- Framework adapters: LangChain, CrewAI, LlamaIndex, plus a generic Python function wrapper.
- Fail-closed runtime: default-deny if policy evaluation fails or evidence is missing/stale.
- Signed, short-lived clearance tokens (issued on allow, verifiable by downstream services without calling back to Evidence Gate).
- Hash-chained audit log with tamper-evidence guarantees.
- Policy authoring workflow: LLM-assisted drafting from SOP text, but require explicit human approval before a policy is live — never silent activation.

**Phase 3 — Scale (Enterprise)**
- SSO integration for policy approval workflows.
- Multi-tenant / multi-team policy packs with inheritance and overrides.
- Verifiable clearance-token SDK for third-party/downstream services.
- Compliance-ready exports (SOC2-style evidence packages).
- Webhooks/alerting on block or review events.
- Policy version control, diffing, and rollback.
- Self-hosted / VPC deployment option for data-sensitive customers.

### 5.5 Success Metrics
See Section 2 (North Star + supporting metrics). Add at GA: number of tools wrapped in production per customer, and % of customers who move from simulate → enforce within 30 days (core adoption funnel health check).

### 5.6 Risks / Open Questions
- **Policy authoring complexity** — if writing correct YAML policy is hard, adoption stalls at the Author step. Needs strong LLM-assisted drafting + validation/linting.
- **Runtime latency** — enforcement must be fast enough that teams don't route around it under load.
- **Coverage gaps** — agents can potentially reach a sensitive action through an unwrapped path; needs a way to surface "known-unwrapped" tools as a residual-risk report, not just silently miss them.
- **Token/key management** — clearance token signing keys are a new piece of security infrastructure; needs a clear rotation/revocation story.
- **Trust calibration** — diagnostics in simulate mode must closely predict what enforce mode would actually do, or teams lose confidence during the handoff.

---

## 6. Naming Recommendations

"Evidence Gate" is clear but generic — it reads like a feature name, not a product/company name, and "Gate" is heavily overused in security branding. Alternatives, ranked:

| Name | Why it works | Watch-outs |
|---|---|---|
| **Marshal** *(top pick)* | Double meaning: "to marshal evidence" (assemble facts in support of a claim) *and* a marshal is literally the law-enforcement figure who executes orders. Matches "deterministic enforcement" almost exactly. Short, verb-able ("Marshal your agent's actions"). | Check domain/trademark availability — common word, likely contested in dev-tools space. |
| **Warrant** | A warrant is authorization granted *because* evidence supports it — nearly a literal restatement of the product's function. Strong, serious, legal-adjacent tone fits compliance buyers. | A prior authz startup used this name (acquired ~2023); could cause confusion in search/branding. |
| **Docket** | Evokes a court docket — a queue of matters to be decided — which maps well to the "review" queue concept specifically. | Weaker fit for the "enforcement" half of the story; leans more observability than firewall. |
| **Corro** (from *corroborate*) | Directly names the core function — corroborating evidence — and is short/brandable. | Less immediately meaningful without explanation; needs a tagline to land. |
| **ProofGate** | Keeps the "Gate" architecture metaphor people may already associate with the space, but "Proof" is more precise and less generic than "Evidence." | Still inherits some of "Gate" fatigue; safer/incremental choice rather than a bold rename. |

**Recommendation:** *Marshal* — it's the only option that carries both halves of the positioning (assembling evidence + enforcing action) in a single word, reads as a strong company name (not just a feature), and works as a verb in marketing copy. Worth a quick domain/trademark check before committing.

