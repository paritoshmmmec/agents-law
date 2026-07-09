# Framework adapters

Three adapters share one seam — a `GatePort` (local or remote) plus a `GateSession`
that accumulates evidence as tools run. Each gates the sensitive tool *in the
agent's own call path*, so `BLOCK`/`REVIEW` raise `ClearanceDenied` before it
executes; all three work against an in-process `Gate` or the remote client
unchanged.

## LangChain

A callback handler that collects evidence tools and gates the sensitive one:

```python
from evidence_gate import Gate, ManifestBuilder, PolicySet
from evidence_gate.integrations.langchain import EvidenceGateCallbackHandler, LocalGatePort

port = LocalGatePort(Gate(PolicySet.from_dir("policies")), builder)   # or RemoteGatePort(RemoteGate(...))
handler = EvidenceGateCallbackHandler(port, action_mapping={"send_*": "marketing.send_sequence"})
agent.invoke(..., config={"callbacks": [handler]})   # BLOCK/REVIEW raise before the tool runs
```

## CrewAI / LlamaIndex

Both frameworks' event buses are observe-only (they can't stop a call), so these
adapters wrap the tool itself and share a `GateSession`:

```python
from evidence_gate.integrations.base import GateSession, LocalGatePort
from evidence_gate.integrations.crewai import gate_crew_tools        # or llamaindex.gate_llama_tools

session = GateSession(LocalGatePort(gate, builder),
                      action_mapping={"send_*": "marketing.send_sequence"}, now=now)
tools = gate_crew_tools([get_optin, send_marketing], session)        # gated, drop-in replacements
```

See `examples/remote_agent.py`, `examples/trace_replay.py`, and
`examples/langchain_agent.py` for each path end-to-end.
