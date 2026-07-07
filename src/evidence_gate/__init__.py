"""Evidence Gate — deterministic runtime enforcement for agent tool calls.

The agent proposes an action and declares the evidence behind it; the gate
evaluates that evidence against explicit policy and returns Allow / Restrict /
Review / Block. No LLM runs inside the gate. See DESIGN.md.
"""

from evidence_gate.audit import AuditLog
from evidence_gate.gate import ActionBlocked, Gate, GateResult
from evidence_gate.policy import Comparison, PolicySet
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
from evidence_gate.signing import Signer, TokenExpired, TokenInvalid, Verifier
from evidence_gate.telemetry import (
    DecisionEvent,
    NullSink,
    OTelSink,
    TelemetrySink,
)
from evidence_gate.trace import Extractor, ManifestBuilder, ToolCall
from evidence_gate.trace_adapters import (
    LANGFUSE,
    LANGSMITH,
    OPENAI,
    NormalizeResult,
    SimReport,
    TraceMapping,
    normalize,
    simulate,
)

__all__ = [
    "ActionBlocked",
    "AuditLog",
    "Comparison",
    "Decision",
    "DecisionEvent",
    "Effect",
    "EvidenceItem",
    "EvidenceManifest",
    "EvidenceSource",
    "Extractor",
    "Gate",
    "GateResult",
    "InMemoryReviewQueue",
    "LANGFUSE",
    "LANGSMITH",
    "ManifestBuilder",
    "NormalizeResult",
    "NullSink",
    "OPENAI",
    "OTelSink",
    "PolicySet",
    "ProposedAction",
    "ReviewQueue",
    "RuleResult",
    "SimReport",
    "Signer",
    "TelemetrySink",
    "TokenExpired",
    "TokenInvalid",
    "ToolCall",
    "TraceMapping",
    "Verifier",
    "normalize",
    "simulate",
]

# Remote surface — depends on the optional `client` / `service` extras. Import
# lazily so the core library stays importable without fastapi / httpx installed.
try:  # pragma: no cover - exercised via the extras
    from evidence_gate.client import (
        ClearanceDenied,
        GateUnreachable,
        RemoteGate,
        RemoteResult,
    )

    __all__ += ["ClearanceDenied", "GateUnreachable", "RemoteGate", "RemoteResult"]
except ImportError:  # httpx not installed (client extra absent)
    pass

try:  # pragma: no cover - exercised via the extras
    from evidence_gate.service import create_app

    __all__ += ["create_app"]
except ImportError:  # fastapi not installed (service extra absent)
    pass

# The framework-neutral GatePort seam lives in integrations.base and depends
# only on the core + client, so it is always importable.
from evidence_gate.integrations.base import (  # noqa: E402
    GatePort,
    GateSession,
    GateVerdict,
    LocalGatePort,
    RemoteGatePort,
)

__all__ += ["GatePort", "GateSession", "GateVerdict", "LocalGatePort", "RemoteGatePort"]

try:  # pragma: no cover - exercised via the extras
    from evidence_gate.integrations.langchain import EvidenceGateCallbackHandler

    __all__ += ["EvidenceGateCallbackHandler"]
except ImportError:  # langchain-core not installed (langchain extra absent)
    pass

try:  # pragma: no cover - exercised via the extras
    from evidence_gate.integrations.crewai import gate_crew_tools

    __all__ += ["gate_crew_tools"]
except ImportError:  # crewai not installed (crewai extra absent)
    pass

try:  # pragma: no cover - exercised via the extras
    from evidence_gate.integrations.llamaindex import gate_llama_tools

    __all__ += ["gate_llama_tools"]
except ImportError:  # llama-index-core not installed (llamaindex extra absent)
    pass
