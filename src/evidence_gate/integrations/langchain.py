"""LangChain adapter (COMPARISON.md §6 #5).

A callback handler that watches the tools a LangChain agent runs, collects the
evidence-bearing ones, and — the moment the agent is about to run a *sensitive*
tool — routes that call through the evidence gate. On BLOCK/REVIEW it raises before
the tool executes; on ALLOW/RESTRICT it steps out of the way. This is the runtime
enforcement point in the callback lifecycle, kept on our own surface.

The handler is transport-agnostic via a small `GatePort` seam: it works against an
in-process `Gate` (`LocalGatePort`) or the fail-closed remote client
(`RemoteGatePort`) with no change to the handler itself.

Needs the `langchain` extra:  uv sync --extra langchain
"""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from langchain_core.callbacks import BaseCallbackHandler

from evidence_gate.client import ClearanceDenied
from evidence_gate.gate import Gate
from evidence_gate.schemas import Decision, Effect, ProposedAction
from evidence_gate.trace import ManifestBuilder, ToolCall


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GateVerdict:
    """The uniform result a `GatePort` returns, whatever the transport underneath."""

    def __init__(
        self,
        effect: Effect,
        decision: Decision,
        review_ticket: str | None = None,
        token: str | None = None,
    ) -> None:
        self.effect = effect
        self.decision = decision
        self.review_ticket = review_ticket
        self.token = token

    @property
    def reason(self) -> str:
        parts = [r.reason for r in self.decision.results if r.effect != Effect.ALLOW]
        return "; ".join(parts) or self.effect.value


@runtime_checkable
class GatePort(Protocol):
    """The one method the handler needs, regardless of local vs. remote gate."""

    def check(
        self, action: ProposedAction, *, tool_calls: list[ToolCall], now: datetime
    ) -> GateVerdict: ...


class LocalGatePort:
    """Route through an in-process `Gate`, building the manifest client-side."""

    def __init__(self, gate: Gate, builder: ManifestBuilder) -> None:
        self._gate = gate
        self._builder = builder

    def check(
        self, action: ProposedAction, *, tool_calls: list[ToolCall], now: datetime
    ) -> GateVerdict:
        manifest = self._builder.build(tool_calls, now)
        result = self._gate.check(action, manifest, now=now)
        return GateVerdict(
            effect=result.effect,
            decision=result.decision,
            review_ticket=result.review_ticket,
        )


class RemoteGatePort:
    """Route through the fail-closed `RemoteGate`; the service reconstructs evidence.

    A `GateUnreachable` from the client propagates unchanged (fail closed — the
    handler must not let the tool run). A BLOCK arrives as a `ClearanceDenied`,
    which we normalize back into a BLOCK `GateVerdict` so the handler stays the
    single place that decides to raise.
    """

    def __init__(self, remote: Any) -> None:
        self._remote = remote

    def check(
        self, action: ProposedAction, *, tool_calls: list[ToolCall], now: datetime
    ) -> GateVerdict:
        try:
            result = self._remote.check(action, tool_calls=tool_calls, now=now)
        except ClearanceDenied as denied:
            return GateVerdict(effect=Effect.BLOCK, decision=denied.decision)
        return GateVerdict(
            effect=result.effect,
            decision=result.decision,
            review_ticket=result.review_ticket,
            token=result.token,
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
        self._port = port
        self._action_mapping = action_mapping
        self._actor = actor
        self._now = now
        self._pending: dict[str, tuple[str, dict[str, Any]]] = {}  # run_id -> (tool, args)
        self.evidence: list[ToolCall] = []

    # -- helpers ---------------------------------------------------------------
    def _match_action(self, tool: str) -> str | None:
        for pattern, action in self._action_mapping.items():
            if fnmatch.fnmatch(tool, pattern):
                return action
        return None

    @staticmethod
    def _coerce_result(output: Any) -> Any:
        """Best-effort normalize a tool output into something an extractor can read."""
        content = getattr(output, "content", output)  # unwrap ToolMessage
        if isinstance(content, str):
            try:
                return json.loads(content)
            except (ValueError, TypeError):
                return content
        return content

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

        action_id = self._match_action(tool)
        if action_id is None:
            return  # evidence tool: nothing to gate; collected on_tool_end

        now = self._now or _utcnow()
        action = ProposedAction(
            action=action_id,
            payload=args,
            actor=self._actor,
            request_id=str(run_id),
        )
        verdict = self._port.check(action, tool_calls=list(self.evidence), now=now)

        if verdict.effect == Effect.BLOCK:
            raise ClearanceDenied(verdict.decision, verdict.reason, str(run_id))
        if verdict.effect == Effect.REVIEW:
            ticket = verdict.review_ticket or "pending"
            raise ClearanceDenied(
                verdict.decision, f"routed for review ({ticket}): {verdict.reason}", str(run_id)
            )
        # ALLOW / RESTRICT: return and let LangChain run the tool.

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> Any:
        entry = self._pending.pop(str(run_id), None)
        if entry is None:
            return
        tool, args = entry
        now = self._now or _utcnow()
        # Record every completed call; the ManifestBuilder only extracts evidence
        # from tools with a registered extractor, so this stays opt-in.
        self.evidence.append(
            ToolCall(
                tool=tool,
                args=args,
                result=self._coerce_result(output),
                call_id=str(run_id),
                observed_at=now,
            )
        )

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> Any:
        self._pending.pop(str(run_id), None)
