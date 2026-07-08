"""Shared plumbing for the framework adapters (COMPARISON.md §6 #5).

Every adapter — LangChain, CrewAI, LlamaIndex — does the same three things in the
same order: collect the evidence-bearing tool calls an agent makes, and the moment
it is about to run a *sensitive* tool, route that call through the evidence gate,
raising before it executes on BLOCK/REVIEW and stepping aside on ALLOW/RESTRICT.
The only thing that differs per framework is *where the hook is* and how a tool's
name/args/result are spelled.

So the framework-specific files stay tiny: they translate their framework's hook
into calls on a `GateSession`, which owns all of the actual gating logic here and
is fully testable with no framework installed. The transport underneath the
session is a `GatePort` (in-process `Gate` or the fail-closed remote client) — the
same seam the LangChain handler already used, lifted here so all three share it.
"""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from evidence_gate.errors import ClearanceDenied
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
    """The one method the adapters need, regardless of local vs. remote gate."""

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
    adapter must not let the tool run). A BLOCK arrives as a `ClearanceDenied`,
    which we normalize back into a BLOCK `GateVerdict` so the session stays the
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


def coerce_result(output: Any) -> Any:
    """Best-effort normalize a tool output into something an extractor can read.

    Unwraps a `.content` attribute (LangChain `ToolMessage`, LlamaIndex
    `ToolOutput`) and JSON-decodes a string payload. A non-JSON string is
    returned unchanged — extractors decide what they can use.
    """
    content = getattr(output, "content", output)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    return content


class GateSession:
    """Framework-neutral evidence collection + gating for one agent run.

    An adapter feeds it two kinds of event, in the order the agent produces them:

      * `enforce(tool, args, request_id)` when a tool is *about to run*. If the
        tool matches `action_mapping` it is sensitive: the call is gated against
        the evidence gathered so far. On BLOCK/REVIEW this raises
        `ClearanceDenied` (the adapter's hook must run in a path where raising
        prevents execution); on ALLOW/RESTRICT it returns the `GateVerdict`. A
        non-sensitive tool returns `None` — nothing to gate.
      * `record(tool, args, result, call_id)` when a tool *finishes*. Every
        completed call is recorded; the `ManifestBuilder` only extracts evidence
        from tools with a registered extractor, so recording stays opt-in.

    `now` is injected so gated/tested runs are reproducible, mirroring the
    engine's determinism contract. The session holds no framework types — the
    adapters convert to/from these plain calls.
    """

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
        self.evidence: list[ToolCall] = []

    def match_action(self, tool: str) -> str | None:
        """The gate action a `tool` maps to, or None if it isn't sensitive."""
        for pattern, action in self._action_mapping.items():
            if fnmatch.fnmatch(tool, pattern):
                return action
        return None

    def enforce(
        self, tool: str, args: dict[str, Any], request_id: str
    ) -> GateVerdict | None:
        """Gate a sensitive tool before it runs. Raises on BLOCK/REVIEW.

        Returns the ALLOW/RESTRICT verdict for a sensitive tool, or None for a
        tool that maps to no action (nothing to gate).
        """
        action_id = self.match_action(tool)
        if action_id is None:
            return None

        now = self._now or _utcnow()
        action = ProposedAction(
            action=action_id, payload=args, actor=self._actor, request_id=request_id
        )
        verdict = self._port.check(action, tool_calls=list(self.evidence), now=now)

        if verdict.effect == Effect.BLOCK:
            raise ClearanceDenied(verdict.decision, verdict.reason, request_id)
        if verdict.effect == Effect.REVIEW:
            ticket = verdict.review_ticket or "pending"
            raise ClearanceDenied(
                verdict.decision, f"routed for review ({ticket}): {verdict.reason}", request_id
            )
        return verdict  # ALLOW / RESTRICT

    def record(
        self,
        tool: str,
        args: dict[str, Any],
        result: Any,
        call_id: str,
        observed_at: datetime | None = None,
    ) -> None:
        """Record a completed tool call as candidate evidence."""
        self.evidence.append(
            ToolCall(
                tool=tool,
                args=args,
                result=coerce_result(result),
                call_id=call_id,
                observed_at=observed_at or self._now or _utcnow(),
            )
        )
