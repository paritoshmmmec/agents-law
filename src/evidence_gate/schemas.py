"""Core data model for the evidence gate.

These types are the contract between the (probabilistic) agent and the
(deterministic) gate. The agent fills in a `ProposedAction` and an
`EvidenceManifest`; the gate returns a `Decision`. See DESIGN.md §4.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvidenceSource(str, Enum):
    """Where a fact came from — its provenance.

    Rules can restrict which sources are allowed to justify an action, so this
    is not just metadata: `INFERENCE` and `MEMORY` are weaker than a fresh
    `TOOL_RESULT`, and a policy may reject them outright.
    """

    TOOL_RESULT = "tool_result"  # observed from a tool / API call
    RETRIEVAL = "retrieval"  # RAG / document store
    USER_INPUT = "user_input"  # stated in the conversation
    MEMORY = "memory"  # agent long-term memory / stored profile field
    INFERENCE = "inference"  # derived by the agent, not directly observed


class EvidenceItem(BaseModel):
    """One fact the agent relied on when building its action payload."""

    id: str  # stable id, referenced back by RuleResult.evidence_refs
    claim: str  # human-readable fact, e.g. "user opted in to marketing"
    key: str  # machine key the rules match on, e.g. "marketing.opt_in"
    value: Any  # the fact's value, e.g. True / 4999 / "US"
    source: EvidenceSource
    source_id: str  # provenance handle: doc id, tool call id, message id
    observed_at: datetime  # when the underlying fact was true / fetched
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    observed: bool = True  # True = directly observed, False = inferred


class EvidenceManifest(BaseModel):
    """The full evidence lineage backing a single proposed action.

    Requirement #2: no state-changing action is allowed without one of these.
    """

    items: list[EvidenceItem] = Field(default_factory=list)
    # Conflicts the agent itself noticed. The engine also detects conflicts
    # independently (DESIGN §5.3) — this list is corroborating signal, not the
    # source of truth.
    declared_conflicts: list[tuple[str, str]] = Field(default_factory=list)
    compiled_at: datetime

    def by_key(self, key: str) -> list[EvidenceItem]:
        """All evidence items recorded under `key` (may be empty)."""
        return [item for item in self.items if item.key == key]


class ProposedAction(BaseModel):
    """What the agent wants to do — the thing the gate is asked to allow."""

    action: str  # canonical action id, e.g. "marketing.send_sequence"
    payload: dict[str, Any]  # the assembled tool arguments
    actor: str  # agent / principal id (RBAC still applies upstream)
    request_id: str  # idempotency + audit correlation


class Effect(str, Enum):
    """The gate's verdict for a single rule or the aggregate decision.

    Ordering matters: aggregation is most-restrictive-wins over this lattice
    (DESIGN §5.4). `_order` encodes BLOCK > REVIEW > RESTRICT > ALLOW.
    """

    ALLOW = "allow"  # execute as-is
    RESTRICT = "restrict"  # execute a degraded / limited variant
    REVIEW = "review"  # route to human / eval; do not execute yet
    BLOCK = "block"  # refuse

    @property
    def severity(self) -> int:
        return _EFFECT_ORDER[self]


_EFFECT_ORDER: dict[Effect, int] = {
    Effect.ALLOW: 0,
    Effect.RESTRICT: 1,
    Effect.REVIEW: 2,
    Effect.BLOCK: 3,
}


class RuleResult(BaseModel):
    """Outcome of evaluating one rule — kept for explainability."""

    rule_id: str
    effect: Effect
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)  # EvidenceItem ids


class Decision(BaseModel):
    """The gate's final answer for one `check()` call."""

    effect: Effect  # aggregate (most restrictive of `results`)
    results: list[RuleResult]  # every rule that fired, in order
    request_id: str
    decided_at: datetime
    policy_version: str | None = None  # set by the gate once known
