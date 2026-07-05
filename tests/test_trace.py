"""ManifestBuilder: derive a manifest from recorded tool calls (DESIGN §12.1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from evidence_gate import (
    Effect,
    EvidenceItem,
    EvidenceSource,
    Gate,
    ManifestBuilder,
    PolicySet,
    ProposedAction,
    ToolCall,
)

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    """Map a get_optin tool result into an opt-in evidence item."""
    result = call.result
    if not result or not result.get("found"):
        return []
    return [
        EvidenceItem(
            id=f"optin-{call.call_id}",
            claim="marketing opt-in",
            key="marketing.opt_in",
            value=result["value"],
            source=EvidenceSource.TOOL_RESULT,
            source_id=call.call_id,
            observed_at=call.observed_at,
            observed=True,
        )
    ]


def _builder() -> ManifestBuilder:
    return ManifestBuilder().register("get_optin", optin_extractor)


def test_builder_extracts_registered_tool():
    calls = [
        ToolCall(
            tool="get_optin",
            args={"contact_id": 42},
            result={"found": True, "value": True},
            call_id="c1",
            observed_at=NOW - timedelta(days=60),
        )
    ]
    manifest = _builder().build(calls, compiled_at=NOW)
    assert len(manifest.items) == 1
    assert manifest.items[0].key == "marketing.opt_in"
    assert manifest.items[0].source_id == "c1"


def test_unregistered_tool_contributes_nothing():
    calls = [
        ToolCall(tool="unknown_tool", result={"x": 1}, call_id="c1", observed_at=NOW)
    ]
    manifest = _builder().build(calls, compiled_at=NOW)
    assert manifest.items == []


def test_extractor_can_yield_zero_items():
    calls = [
        ToolCall(tool="get_optin", result={"found": False}, call_id="c1", observed_at=NOW)
    ]
    manifest = _builder().build(calls, compiled_at=NOW)
    assert manifest.items == []


def test_build_is_deterministic_in_order():
    calls = [
        ToolCall(
            tool="get_optin",
            result={"found": True, "value": True},
            call_id=f"c{i}",
            observed_at=NOW - timedelta(days=i),
        )
        for i in range(3)
    ]
    first = _builder().build(calls, compiled_at=NOW)
    again = _builder().build(calls, compiled_at=NOW)
    assert first.model_dump() == again.model_dump()
    assert [it.source_id for it in first.items] == ["c0", "c1", "c2"]


def test_derived_manifest_drives_the_gate():
    # The whole point: a trace-derived manifest is evaluated identically to an
    # agent-supplied one. Fresh opt-in -> ALLOW.
    calls = [
        ToolCall(
            tool="get_optin",
            result={"found": True, "value": True},
            call_id="c1",
            observed_at=NOW - timedelta(days=60),
        )
    ]
    manifest = _builder().build(calls, compiled_at=NOW)
    gate = Gate(PolicySet.from_dir("policies"))
    action = ProposedAction(
        action="marketing.send_sequence",
        payload={"contact_id": 42},
        actor="agent",
        request_id="r1",
    )
    result = gate.check(action, manifest, now=NOW)
    assert result.effect is Effect.ALLOW


def test_stale_derived_manifest_reviews():
    calls = [
        ToolCall(
            tool="get_optin",
            result={"found": True, "value": True},
            call_id="c1",
            observed_at=NOW - timedelta(days=425),
        )
    ]
    manifest = _builder().build(calls, compiled_at=NOW)
    gate = Gate(PolicySet.from_dir("policies"))
    action = ProposedAction(
        action="marketing.send_sequence",
        payload={"contact_id": 42},
        actor="agent",
        request_id="r1",
    )
    result = gate.check(action, manifest, now=NOW)
    assert result.effect is Effect.REVIEW
