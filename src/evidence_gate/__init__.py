"""Evidence Gate — deterministic runtime enforcement for agent tool calls.

The agent proposes an action and declares the evidence behind it; the gate
evaluates that evidence against explicit policy and returns Allow / Restrict /
Review / Block. No LLM runs inside the gate. See DESIGN.md.
"""

from evidence_gate.audit import AuditLog
from evidence_gate.gate import ActionBlocked, Gate, GateResult
from evidence_gate.policy import PolicySet
from evidence_gate.review import InMemoryReviewQueue, ReviewQueue
from evidence_gate.schemas import (
    Decision,
    Effect,
    EvidenceItem,
    EvidenceManifest,
    EvidenceSource,
    ProposedAction,
    RuleResult,
)

__all__ = [
    "ActionBlocked",
    "AuditLog",
    "Decision",
    "Effect",
    "EvidenceItem",
    "EvidenceManifest",
    "EvidenceSource",
    "Gate",
    "GateResult",
    "InMemoryReviewQueue",
    "PolicySet",
    "ProposedAction",
    "ReviewQueue",
    "RuleResult",
]
