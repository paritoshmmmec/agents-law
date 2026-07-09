# Vision, PRD & North Star

## 1. Product vision

Evidence Gate is the **evidence firewall for agentic software**: a small,
deterministic layer that sits between an AI agent and any consequential action, and
refuses to let that action proceed unless the agent can show fresh, authorized,
non-conflicting evidence that it's justified.

> **RBAC answers "is this agent allowed to use this tool, ever?" Evidence Gate
> answers "is this specific call, right now, backed by facts the agent was actually
> entitled to rely on?"**

That distinction is the whole product. Everything else — replay, policy authoring,
clearance tokens, audit trails — exists to make that one question cheap to ask and
impossible to fake.

**The adoption path is the product.** Teams don't need to trust Evidence Gate
before they can use it:

1. Point it at yesterday's traces → see what would have been allowed, restricted,
   sent to review, or blocked, with reasons. Zero production risk.
2. Wrap one sensitive tool by name pattern → move that single tool from simulated to
   live, fail-closed enforcement.
3. Expand tool-by-tool, with every decision hash-chained and every allowed action
   carrying a short-lived, verifiable clearance token.

No rewrite, no framework migration, no "trust the black box" leap of faith.

## 2. North Star

> Can we prove why this agent took this action, using facts it was actually allowed
> to rely on?

If the honest answer is no, the action is blocked, restricted, or routed to a human
— **before** it executes, not after.

**North Star Metric:** % of consequential agent actions that execute with a valid,
time-fresh, non-conflicting evidence chain attached — target 100% for wrapped tools.

| Supporting metric | Why it matters |
|---|---|
| Time-to-block for unjustified actions | Target: 0 (pre-execution, not detected after the fact) |
| False-restriction rate | Guards against the system becoming security theater teams route around |
| Time from "connect traces" to first diagnostic report | Proxy for adoption friction — minutes, not days |
| % of allowed actions with a verifiable clearance token consumed downstream | Whether enforcement is load-bearing, not decorative |

**Anti-metric to watch:** latency added per tool call. A deterministic gate that's
slow enough to get bypassed defeats its own purpose.

## 3. Positioning

Evidence Gate is **not**:

- an agent framework (LangChain, CrewAI, etc. sit upstream of it)
- an observability/tracing tool (it consumes traces, doesn't replace them)
- an RBAC/IAM replacement (it assumes authz already happened)
- an LLM-based judge (enforcement is deterministic, compiled policy — never a
  model's opinion at runtime)

It **is** the deterministic control point that existing tools miss:

> The agent is authorized. Is this action actually justified by the evidence?

## 4. Target users

- **Platform/infra engineer** building an internal agent platform who needs a
  control point that doesn't require rewriting every tool integration.
- **Security or compliance lead** who needs to answer "why did the agent do that"
  for an auditor, not just "what did it do."
- **AI product engineer** shipping an agent that touches money, infra, or customer
  data and wants a fail-closed guardrail without building one from scratch.
- **Auditor/compliance reviewer** who needs tamper-evident, replayable decision logs
  without access to raw prompts.

## 5. Core product flow

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

### Functional requirements by phase

**Phase 1 — Simulate (MVP).** Trace ingestion adapters (LangSmith, OpenAI,
Langfuse, generic JSON); policy compiler (YAML → deterministic rule set);
diagnostic engine classifying each historical action with a human-readable reason;
a reporting surface (CLI + minimal dashboard) summarizing coverage and findings.

**Phase 2 — Enforce (V1).** Tool-wrapping SDK (decorator/middleware matching on
tool name pattern); framework adapters (LangChain, CrewAI, LlamaIndex, generic
Python); fail-closed runtime (default-deny if policy evaluation fails or evidence is
missing/stale); signed, short-lived clearance tokens; hash-chained audit log;
policy authoring workflow (LLM-assisted drafting from SOP text, explicit human
approval before a policy is live — never silent activation).

**Phase 3 — Scale (Enterprise).** SSO for policy approval; multi-tenant policy
packs with inheritance and overrides; verifiable clearance-token SDK for
third-party services; compliance-ready exports; webhooks/alerting on block or review
events; policy version control, diffing, and rollback; self-hosted / VPC deployment.

### Risks / open questions

- **Policy authoring complexity** — if writing correct YAML policy is hard, adoption
  stalls at the Author step. Needs strong LLM-assisted drafting + linting.
- **Runtime latency** — enforcement must be fast enough that teams don't route
  around it under load.
- **Coverage gaps** — agents can reach a sensitive action through an unwrapped path;
  needs a residual-risk report surfacing "known-unwrapped" tools, not a silent miss.
- **Token/key management** — clearance-token signing keys are new security
  infrastructure; needs a clear rotation/revocation story.
- **Trust calibration** — diagnostics in simulate mode must closely predict what
  enforce mode would actually do, or teams lose confidence during the handoff.
