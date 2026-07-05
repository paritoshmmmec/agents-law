"""Human-in-the-loop routing (DESIGN.md §8).

When a decision is REVIEW, the gate hands the *full assembled context* (action +
manifest + decision) to a `ReviewQueue` and returns without raising — so the
agent's loop keeps running. A human or a separate eval agent resolves the ticket
later; the resolution is itself auditable.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from evidence_gate.schemas import Decision, Effect, EvidenceManifest, ProposedAction


class ReviewTicket(BaseModel):
    """A pending action parked for human/eval review. Context is never dropped."""

    ticket_id: str
    action: ProposedAction
    manifest: EvidenceManifest
    decision: Decision
    resolved: bool = False
    approver: str | None = None
    resolved_effect: Effect | None = None


class ReviewQueue(Protocol):
    """The routing seam. Swap the in-memory impl for a real queue in production."""

    def enqueue(
        self,
        decision: Decision,
        action: ProposedAction,
        manifest: EvidenceManifest,
    ) -> str: ...

    def resolve(self, ticket_id: str, approver: str, effect: Effect) -> ReviewTicket: ...


class InMemoryReviewQueue:
    """Minimal queue for the demo and tests."""

    def __init__(self) -> None:
        self._tickets: dict[str, ReviewTicket] = {}
        self._counter = 0

    def enqueue(
        self,
        decision: Decision,
        action: ProposedAction,
        manifest: EvidenceManifest,
    ) -> str:
        self._counter += 1
        ticket_id = f"rev-{self._counter:04d}"
        self._tickets[ticket_id] = ReviewTicket(
            ticket_id=ticket_id,
            action=action,
            manifest=manifest,
            decision=decision,
        )
        return ticket_id

    def resolve(self, ticket_id: str, approver: str, effect: Effect) -> ReviewTicket:
        ticket = self._tickets[ticket_id]
        ticket.resolved = True
        ticket.approver = approver
        ticket.resolved_effect = effect
        return ticket

    def get(self, ticket_id: str) -> ReviewTicket:
        return self._tickets[ticket_id]

    def pending(self) -> list[ReviewTicket]:
        return [t for t in self._tickets.values() if not t.resolved]
