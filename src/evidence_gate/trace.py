"""Trace-derived manifests (DESIGN §12.1).

The agent-supplied path (the agent hands the gate an `EvidenceManifest`) is one
way in. This is the other: assemble a manifest from the *record of tool calls the
agent actually made*, so the evidence lineage is derived from observed execution
rather than from the agent's self-report.

Both paths converge on the same validated `EvidenceManifest` before the engine
runs — this file adds no new evaluation logic, only a way to *build* the manifest.
It is deliberately explicit: you register a named `Extractor` per tool that maps
that tool's recorded result into evidence items. No schema-guessing, no LLM — an
extractor is ordinary deterministic code, and a tool with no extractor
contributes nothing (rather than silently inventing evidence).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, Field

from evidence_gate.schemas import EvidenceItem, EvidenceManifest


class ToolCall(BaseModel):
    """One recorded tool invocation from an agent's trace.

    This is the minimal shape a LangSmith/Langfuse/OpenAI-log adapter would
    normalize into; keeping it small keeps the builder free of any vendor lock-in.
    """

    tool: str  # tool name, e.g. "get_optin"
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None  # whatever the tool returned (already parsed)
    call_id: str  # stable id -> flows into EvidenceItem.source_id
    observed_at: datetime  # when the tool actually ran


# An extractor turns one tool call into zero or more evidence items. It is pure
# and deterministic — the whole point of the seam.
Extractor = Callable[[ToolCall], list[EvidenceItem]]


class ManifestBuilder:
    """Assembles an `EvidenceManifest` from recorded tool calls via extractors.

    Register one extractor per tool whose output is evidence. `build()` runs each
    recorded call through its extractor (if any) and collects the items. A call
    with no registered extractor is ignored — evidence is opt-in, never inferred.
    """

    def __init__(self) -> None:
        self._extractors: dict[str, Extractor] = {}

    def register(self, tool: str, extractor: Extractor) -> ManifestBuilder:
        """Register the extractor for `tool`. Returns self for chaining."""
        self._extractors[tool] = extractor
        return self

    def build(self, calls: list[ToolCall], compiled_at: datetime) -> EvidenceManifest:
        """Derive a manifest from a trace. Deterministic in `calls` order."""
        items: list[EvidenceItem] = []
        for call in calls:
            extractor = self._extractors.get(call.tool)
            if extractor is None:
                continue
            items.extend(extractor(call))
        return EvidenceManifest(items=items, compiled_at=compiled_at)
