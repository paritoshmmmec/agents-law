# Quick start

```bash
uv sync                         # install core deps into .venv
uv run examples/demo_agent.py   # marketing tripwire: the four failure modes
uv run examples/refund_agent.py # refund tripwire: cross-key compare + RESTRICT
uv run pytest                   # 127 tests: failure modes, determinism, audit, remote, adapters, telemetry, cli
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
