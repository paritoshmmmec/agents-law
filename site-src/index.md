# Evidence Gate

!!! info "Looking for the overview?"
    The marketing landing page lives at **[the site root](../)**. These pages are
    the developer documentation.

> **Status: alpha (v0.4.1).** The core gate, engine, audit chain, cross-key
> `compare` rules, a real RESTRICT degradation path, and a live LLM agent work
> today; v0.3 added the integration surface (remote HTTP gate service, fail-closed
> client, HMAC-signed clearance tokens, Trace-to-Gate replay, LangChain adapter);
> v0.4 added per-vendor trace presets, CrewAI/LlamaIndex adapters, and OTel decision
> telemetry; and v0.4.1 adds the adoption CLI, a residual-risk coverage report, and
> downstream token enforcement. See the [Roadmap](roadmap.md).

A deterministic runtime enforcement layer that forces an agent to prove its
reasoning against ground-truth evidence **before** a state-changing action is
committed.

RBAC answers *"can this agent call this tool?"* It never answers *"is the
reasoning behind **this** call sound?"* An agent that hallucinates a request or
acts on stale data still emits a valid, authorized payload — a high-confidence
bad decision. The Evidence Gate closes that hole: it sits on the tool-call path,
demands an **Evidence Manifest** for every sensitive action, and evaluates that
evidence against explicit rules with **no LLM in the loop**.

## Where to go next

<div class="grid cards" markdown>

- :material-sitemap: **[Architecture](architecture.md)** — the pure decision seam and how every path converges on one engine.
- :material-rocket-launch: **[Quick start](quickstart.md)** — install, run the demo agents, run the tests.
- :material-robot: **[Running a real agent](running-an-agent.md)** — put a live LLM behind the gate.
- :material-cog: **[Usage & policies](usage.md)** — `check()`, `@enforce`, cross-key `compare` rules, trace-derived manifests.
- :material-server-network: **[Remote gate](remote-gate.md)** — the FastAPI service and the fail-closed client.
- :material-history: **[Trace-to-Gate replay](trace-replay.md)** — replay recorded traces before wiring anything live.
- :material-console: **[CLI](cli.md)** — `evidence-gate replay` / `audit verify`.
- :material-puzzle: **[Framework adapters](adapters.md)** — LangChain, CrewAI, LlamaIndex.
- :material-chart-line: **[Decision telemetry](telemetry.md)** — payload-safe OTel span events.

</div>

The canonical, always-current sources are [`README.md`](https://github.com/paritoshmmmec/agents-law/blob/main/README.md)
and [`DESIGN.md`](https://github.com/paritoshmmmec/agents-law/blob/main/DESIGN.md) in the repo.
