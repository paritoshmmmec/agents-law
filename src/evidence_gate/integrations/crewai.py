"""CrewAI adapter (COMPARISON.md §6 #5).

CrewAI's event bus (`ToolUsageStartedEvent` etc.) is **observe-only** — the bus
dispatches handlers on a thread pool and swallows any exception they raise, so a
listener *cannot* stop a tool from running. Enforcement therefore has to sit where
the agent actually calls the tool: inside `BaseTool._run`. So this adapter wraps a
CrewAI tool in a `BaseTool` whose `_run` gates the call through the shared
`GateSession` before delegating — raising `ClearanceDenied` on BLOCK/REVIEW stops
execution in the agent's own call path.

Evidence flows the same way as every other adapter: a wrapped evidence tool records
its result into the shared session on the way out; a wrapped sensitive tool is
gated against that evidence on the way in. Give every tool the crew uses the same
session so their evidence accumulates.

    session = GateSession(LocalGatePort(gate, builder),
                          action_mapping={"send_*": "marketing.send_sequence"}, now=now)
    tools = gate_crew_tools([get_optin, send_marketing], session)

Needs the `crewai` extra:  uv pip install crewai
"""

from __future__ import annotations

from typing import Any

from crewai.tools import BaseTool

from evidence_gate.integrations.base import GateSession


class GatedTool(BaseTool):
    """A CrewAI `BaseTool` that gates its inner tool through a `GateSession`.

    Sensitive tools (matching the session's `action_mapping`) are gated in `_run`
    and only execute on ALLOW/RESTRICT; everything else runs and has its result
    recorded as candidate evidence. Wrapping preserves the inner tool's name,
    description, and args schema so the agent sees no difference.
    """

    # Declared so pydantic (BaseTool is a BaseModel) accepts them as fields.
    inner: Any = None
    session: GateSession = None  # type: ignore[assignment]

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        # Gate first: raises ClearanceDenied on BLOCK/REVIEW before the tool runs.
        request_id = str(kwargs.get("request_id") or f"{self.name}-call")
        self.session.enforce(self.name, dict(kwargs), request_id=request_id)

        result = self.inner.run(*args, **kwargs)
        # Record every completed call; only tools with a registered extractor
        # contribute evidence, so this stays opt-in.
        self.session.record(self.name, dict(kwargs), result, call_id=request_id)
        return result


def gate_tool(tool: BaseTool, session: GateSession) -> GatedTool:
    """Wrap one CrewAI `BaseTool` so its calls route through `session`."""
    return GatedTool(
        name=tool.name,
        description=tool.description,
        args_schema=getattr(tool, "args_schema", None),
        inner=tool,
        session=session,
    )


def gate_crew_tools(tools: list[BaseTool], session: GateSession) -> list[GatedTool]:
    """Wrap every tool a crew uses so they share one evidence `session`.

    Sensitive tools get gated; evidence tools get recorded. Pass the returned list
    to your `Agent(tools=...)`.
    """
    return [gate_tool(t, session) for t in tools]
