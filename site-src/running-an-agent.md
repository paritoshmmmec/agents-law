# Running a real agent

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
contact 42  fresh opt-in       -> ALLOW   (executed)
contact 77  14-month-old opt-in -> REVIEW  (ticket queued, not executed)
contact 99  no record           -> BLOCK   (not executed)
```
