"""Decision telemetry — OTel span events with deliberate payload hygiene (COMPARISON.md §6 #6).

Every `gate.check()` can emit one telemetry event describing *what the gate
decided*, never *what the agent was doing*. The distinction is the whole point:
an observability trail must be safe to ship to a third-party backend, so the event
carries only non-sensitive scalars — the action id, the verdict, the policy
version, which rules fired and how, and the evidence *keys* and *counts* — and
**never** the payload args, the prompt, the model output, or an evidence item's
*value* or human-readable claim. Those are exactly what a leak would expose, and
they add nothing to a decision dashboard.

Two seams, mirroring the rest of the library:

  * `DecisionEvent.from_decision()` builds the hygienic attribute set from a
    `Decision` + `ProposedAction` + `EvidenceManifest`. It is pure and has no OTel
    dependency, so the hygiene boundary is unit-testable on its own.
  * `TelemetrySink` is where an event goes. `OTelSink` adds it as a span event on
    the current span *iff* OpenTelemetry is installed and a span is recording;
    absent that it is a silent no-op, so telemetry is opt-in and dependency-free.

`emitted_at` is injected (like the engine's `now`) so tests are reproducible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from evidence_gate.schemas import Decision, Effect, EvidenceManifest, ProposedAction

# Event names, matching the COMPARISON §6 #6 naming (our own namespace).
DECISION_EVENT = "evidence_gate.decision"
PENDING_REVIEW_EVENT = "evidence_gate.pending_review"


class DecisionEvent(BaseModel):
    """A hygienic, ship-safe description of one gate decision.

    Everything here is either a bounded scalar or a list of machine keys/ids —
    nothing that reveals the *content* of the action or the evidence. `attributes`
    flattens it into the primitive-only shape OTel attributes require.
    """

    name: str  # DECISION_EVENT or PENDING_REVIEW_EVENT
    request_id: str
    action: str
    effect: Effect
    policy_version: str | None
    rule_ids: list[str] = Field(default_factory=list)  # rules that fired, in order
    failed_rule_ids: list[str] = Field(default_factory=list)  # rules not ALLOW
    evidence_keys: list[str] = Field(default_factory=list)  # keys present, deduped
    evidence_count: int = 0
    review_ticket: str | None = None

    @classmethod
    def from_decision(
        cls,
        action: ProposedAction,
        manifest: EvidenceManifest,
        decision: Decision,
        review_ticket: str | None = None,
    ) -> DecisionEvent:
        """Build the event from a decision. Records keys and counts, never values."""
        # Dedupe evidence keys while preserving first-seen order.
        seen: dict[str, None] = {}
        for item in manifest.items:
            seen.setdefault(item.key, None)
        name = (
            PENDING_REVIEW_EVENT if decision.effect == Effect.REVIEW else DECISION_EVENT
        )
        return cls(
            name=name,
            request_id=decision.request_id,
            action=action.action,
            effect=decision.effect,
            policy_version=decision.policy_version,
            rule_ids=[r.rule_id for r in decision.results],
            failed_rule_ids=[r.rule_id for r in decision.results if r.effect != Effect.ALLOW],
            evidence_keys=list(seen),
            evidence_count=len(manifest.items),
            review_ticket=review_ticket,
        )

    def attributes(self) -> dict[str, Any]:
        """Flatten to OTel-legal attribute primitives (scalars / string sequences).

        Deliberately excludes payload args, evidence values, claims, prompts, and
        model output — see the module docstring. Keys are namespaced so they don't
        collide with other instrumentation on the same span.
        """
        attrs: dict[str, Any] = {
            "evidence_gate.request_id": self.request_id,
            "evidence_gate.action": self.action,
            "evidence_gate.effect": self.effect.value,
            "evidence_gate.rule_ids": self.rule_ids,
            "evidence_gate.failed_rule_ids": self.failed_rule_ids,
            "evidence_gate.evidence_keys": self.evidence_keys,
            "evidence_gate.evidence_count": self.evidence_count,
        }
        if self.policy_version is not None:
            attrs["evidence_gate.policy_version"] = self.policy_version
        if self.review_ticket is not None:
            attrs["evidence_gate.review_ticket"] = self.review_ticket
        return attrs


@runtime_checkable
class TelemetrySink(Protocol):
    """Where a `DecisionEvent` goes. Swap for a test spy or a custom exporter."""

    def emit(self, event: DecisionEvent, *, emitted_at: datetime) -> None: ...


class NullSink:
    """Discards events. The default, so telemetry is strictly opt-in."""

    def emit(self, event: DecisionEvent, *, emitted_at: datetime) -> None:  # noqa: D401
        return None


class OTelSink:
    """Add each event to the current OpenTelemetry span, if one is recording.

    A silent no-op when OpenTelemetry is not installed or no span is active, so
    wiring this sink in never *requires* the otel extra and never fails a gate
    check because telemetry was unavailable. `emitted_at` is passed through as the
    event timestamp (epoch nanoseconds) so replayed decisions stamp correctly.
    """

    def __init__(self) -> None:
        try:  # pragma: no cover - trivial import guard
            from opentelemetry import trace

            self._trace = trace
        except ImportError:  # pragma: no cover
            self._trace = None

    def emit(self, event: DecisionEvent, *, emitted_at: datetime) -> None:
        if self._trace is None:
            return
        span = self._trace.get_current_span()
        # NonRecordingSpan (no active tracer) reports is_recording() == False.
        if not getattr(span, "is_recording", lambda: False)():
            return
        span.add_event(
            event.name,
            attributes=event.attributes(),
            timestamp=int(emitted_at.timestamp() * 1_000_000_000),
        )
