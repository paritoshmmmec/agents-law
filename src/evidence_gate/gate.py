"""The Gate — the enforcement boundary the agent calls before acting.

`check()` does four things, in order (DESIGN.md §6):

  1. Structural validation — reject any action without a well-formed manifest
     (requirement #2), before any policy runs.
  2. Evaluate — deterministic PolicyEngine.evaluate().
  3. Audit — append a hash-chained record *before returning*, so nothing
     executes unrecorded (requirement #5).
  4. Route — on REVIEW, enqueue the full context and return without raising, so
     the agent's loop is not broken (requirement #4).

The gate runs no LLM. It is ordinary deterministic code sitting on the tool-call
path (requirement #1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from evidence_gate import engine
from evidence_gate.audit import AuditLog
from evidence_gate.policy import PolicySet
from evidence_gate.review import InMemoryReviewQueue, ReviewQueue
from evidence_gate.review import ReviewTicket
from evidence_gate.schemas import (
    Decision,
    Effect,
    EvidenceManifest,
    ProposedAction,
    RuleResult,
)


class ActionBlocked(Exception):
    """Raised (by the decorator, or by the caller) when an action is BLOCKed."""

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        reasons = "; ".join(r.reason for r in decision.results if r.effect == decision.effect)
        super().__init__(f"action blocked: {reasons}")


class GateResult:
    """What `check()` returns: the decision plus any review ticket it created."""

    def __init__(self, decision: Decision, review_ticket: str | None = None) -> None:
        self.decision = decision
        self.review_ticket = review_ticket

    @property
    def effect(self) -> Effect:
        return self.decision.effect

    @property
    def allowed(self) -> bool:
        """True when the caller may execute (ALLOW or RESTRICT)."""
        return self.decision.effect in (Effect.ALLOW, Effect.RESTRICT)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Gate:
    def __init__(
        self,
        policy_set: PolicySet,
        audit: AuditLog | None = None,
        review: ReviewQueue | None = None,
    ) -> None:
        self.policies = policy_set
        self.audit = audit if audit is not None else AuditLog()
        self.review = review if review is not None else InMemoryReviewQueue()

    def check(
        self,
        action: ProposedAction,
        manifest: EvidenceManifest | None,
        now: datetime | None = None,
    ) -> GateResult:
        now = now if now is not None else _utcnow()

        # 1. Structural validation. A missing manifest is an automatic BLOCK,
        #    recorded like any other decision — never a silent drop.
        if manifest is None:
            decision = _synthetic_block(
                action.request_id,
                now,
                reason="no evidence manifest supplied (requirement #2)",
            )
            empty = EvidenceManifest(items=[], compiled_at=now)
            self.audit.append(action, empty, decision, now)
            return GateResult(decision)

        # 2. Deterministic evaluation.
        policy = self.policies.get(action.action)
        decision = engine.evaluate(action.action, manifest, policy, now)
        # Stamp the real request id (the engine works from the manifest alone).
        decision.request_id = action.request_id

        # 3. Audit before returning — nothing executes unrecorded.
        self.audit.append(action, manifest, decision, now)

        # 4. Route. REVIEW parks the full context without breaking the loop.
        ticket = None
        if decision.effect == Effect.REVIEW:
            ticket = self.review.enqueue(decision, action, manifest)

        return GateResult(decision, review_ticket=ticket)

    def resolve_review(
        self,
        ticket_id: str,
        approver: str,
        effect: Effect,
        now: datetime | None = None,
    ) -> ReviewTicket:
        """Resolve a parked REVIEW ticket and audit the human override.

        DESIGN §8: the resolution is *itself* audited, with `approver` set. We
        append a fresh, approver-stamped record whose decision carries the
        human-chosen `effect`, so the trail shows both the original REVIEW and
        who overrode it to what.
        """
        now = now if now is not None else _utcnow()
        ticket = self.review.resolve(ticket_id, approver, effect)
        resolved = Decision(
            effect=effect,
            results=[
                RuleResult(
                    rule_id="__review_resolved__",
                    effect=effect,
                    reason=f"review {ticket_id} resolved by {approver}",
                )
            ],
            request_id=ticket.decision.request_id,
            decided_at=now,
            policy_version=ticket.decision.policy_version,
        )
        self.audit.append(ticket.action, ticket.manifest, resolved, now, approver=approver)
        return ticket

    def enforce(self, action: str) -> Callable:
        """Decorator wrapping a tool function with a gate check.

        The wrapped function must be called with a keyword `manifest=` and a
        `payload` dict. On ALLOW/RESTRICT it runs; on REVIEW it returns a
        `GateResult` (pending) without executing; on BLOCK it raises
        `ActionBlocked`. Either way the agent loop keeps control.
        """

        def decorator(fn: Callable) -> Callable:
            def wrapper(
                payload: dict,
                *,
                manifest: EvidenceManifest,
                actor: str = "agent",
                request_id: str,
                now: datetime | None = None,
                **kwargs,
            ):
                proposed = ProposedAction(
                    action=action, payload=payload, actor=actor, request_id=request_id
                )
                result = self.check(proposed, manifest, now=now)
                if result.effect == Effect.BLOCK:
                    raise ActionBlocked(result.decision)
                if result.effect == Effect.REVIEW:
                    return result  # pending human review; do not execute
                # ALLOW / RESTRICT execute. The tool is told which so it can
                # degrade a RESTRICTed payload itself.
                return fn(payload, effect=result.effect, **kwargs)

            wrapper.__name__ = getattr(fn, "__name__", "wrapped")
            wrapper.__doc__ = fn.__doc__
            return wrapper

        return decorator


def _synthetic_block(request_id: str, now: datetime, reason: str) -> Decision:
    return Decision(
        effect=Effect.BLOCK,
        results=[RuleResult(rule_id="__manifest__", effect=Effect.BLOCK, reason=reason)],
        request_id=request_id,
        decided_at=now,
    )
