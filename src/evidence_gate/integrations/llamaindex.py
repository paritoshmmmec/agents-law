"""LlamaIndex adapter (COMPARISON.md §6 #5).

LlamaIndex's instrumentation dispatcher (`BaseEventHandler`, `AgentToolCallEvent`)
is **observe-only**, and its workflow agents don't reliably emit those events —
neither can *prevent* a call. And a `FunctionTool` `callback` runs *after* the tool
executes, so it can rewrite output but not block. Enforcement therefore has to sit
in the tool's own call path.

So this adapter rebuilds a `FunctionTool` whose callable is a gated closure over
the original tool's function: the closure gates the call through the shared
`GateSession` (raising `ClearanceDenied` on BLOCK/REVIEW *before* delegating), then
runs the inner function and records its result as candidate evidence. Because
`FunctionTool.call`/`acall` invoke that callable directly, raising in it stops the
call in the agent's own path. The wrapper preserves the tool's name/description/
schema so the agent sees no difference.

    session = GateSession(LocalGatePort(gate, builder),
                          action_mapping={"send_*": "marketing.send_sequence"}, now=now)
    tools = gate_llama_tools([get_optin, send_marketing], session)

Needs the `llamaindex` extra:  uv pip install llama-index-core
"""

from __future__ import annotations

import functools
from typing import Any

from llama_index.core.tools import FunctionTool

from evidence_gate.integrations.base import GateSession


def gate_tool(tool: FunctionTool, session: GateSession) -> FunctionTool:
    """Wrap one LlamaIndex `FunctionTool` so its calls route through `session`.

    Sensitive tools (matching the session's `action_mapping`) are gated before
    they run and only execute on ALLOW/RESTRICT; everything else runs and has its
    result recorded as candidate evidence.
    """
    name = tool.metadata.name
    inner = tool.fn

    @functools.wraps(inner)
    def gated(*args: Any, **kwargs: Any) -> Any:
        request_id = str(kwargs.get("request_id") or f"{name}-call")
        # Raises ClearanceDenied on BLOCK/REVIEW before the inner fn runs.
        session.enforce(name, dict(kwargs), request_id=request_id)
        result = inner(*args, **kwargs)
        session.record(name, dict(kwargs), result, call_id=request_id)
        return result

    return FunctionTool.from_defaults(
        fn=gated,
        name=name,
        description=tool.metadata.description,
        fn_schema=tool.metadata.fn_schema,
    )


def gate_llama_tools(tools: list[FunctionTool], session: GateSession) -> list[FunctionTool]:
    """Wrap every tool an agent uses so they share one evidence `session`.

    Sensitive tools get gated; evidence tools get recorded. Pass the returned list
    to your agent (`FunctionAgent(tools=...)`, `ReActAgent.from_tools(...)`, …).
    """
    return [gate_tool(t, session) for t in tools]
