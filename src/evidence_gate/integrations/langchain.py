"""LangChain adapter (COMPARISON.md §6 #5).

A callback handler that watches the tools a LangChain agent runs, collects the
evidence-bearing ones, and — the moment the agent is about to run a *sensitive*
tool — routes that call through the evidence gate. On BLOCK/REVIEW it raises before
the tool executes; on ALLOW/RESTRICT it steps out of the way. This is the runtime
enforcement point in the callback lifecycle, kept on our own surface.

The gating logic lives in the framework-neutral `GateSession` (`base.py`); this
file is only the LangChain-specific translation of the callback lifecycle onto it.
The handler is transport-agnostic via the shared `GatePort` seam: it works against
an in-process `Gate` (`LocalGatePort`) or the fail-closed remote client
(`RemoteGatePort`) with no change to the handler itself.

Needs the `langchain` extra:  uv sync --extra langchain
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

# Re-exported so existing imports (`from ...langchain import LocalGatePort`) keep
# working now that the seam lives in base.py.
from evidence_gate.integrations.base import (  # noqa: F401
    GatePort,
    GateSession,
    GateVerdict,
    LocalGatePort,
    RemoteGatePort,
)


class EvidenceGateCallbackHandler(BaseCallbackHandler):
    """Gate the sensitive tool calls a LangChain agent makes.

    `action_mapping` is `{tool_name_glob: action_id}`. A tool whose name matches is
    *sensitive*: it is gated in `on_tool_start` and only runs on ALLOW/RESTRICT. A
    tool that matches nothing is *evidence*: its result is collected in
    `on_tool_end` and feeds the manifest for later sensitive checks.

    `now` is injected so gated/tested runs are reproducible, mirroring the engine's
    determinism contract. `raise_error = True` makes LangChain propagate our
    `ClearanceDenied` rather than swallow it — enforcement, not advice.
    """

    raise_error = True

    def __init__(
        self,
        port: GatePort,
        *,
        action_mapping: dict[str, str],
        actor: str = "agent",
        now: datetime | None = None,
    ) -> None:
        self._session = GateSession(
            port, action_mapping=action_mapping, actor=actor, now=now
        )
        self._pending: dict[str, tuple[str, dict[str, Any]]] = {}  # run_id -> (tool, args)

    @property
    def evidence(self):
        """The evidence collected so far (exposed for tests / introspection)."""
        return self._session.evidence

    # -- callbacks -------------------------------------------------------------
    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        tool = (serialized or {}).get("name", "")
        args = inputs if isinstance(inputs, dict) else {"input": input_str}
        self._pending[str(run_id)] = (tool, args)
        # Raises ClearanceDenied on BLOCK/REVIEW before LangChain runs the tool.
        self._session.enforce(tool, args, request_id=str(run_id))

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> Any:
        entry = self._pending.pop(str(run_id), None)
        if entry is None:
            return
        tool, args = entry
        self._session.record(tool, args, output, call_id=str(run_id))

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> Any:
        self._pending.pop(str(run_id), None)
