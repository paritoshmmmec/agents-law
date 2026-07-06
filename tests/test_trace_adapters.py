"""Trace-to-Gate tests: generic normalize + simulate replay reaches the same verdicts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from evidence_gate import (
    EvidenceItem,
    EvidenceSource,
    Gate,
    ManifestBuilder,
    PolicySet,
    TraceMapping,
    normalize,
    simulate,
)
from evidence_gate.trace import ToolCall

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "marketing.send_sequence"


def _optin_extractor(call: ToolCall) -> list[EvidenceItem]:
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
    return ManifestBuilder().register("get_optin", _optin_extractor)


# --- normalize -------------------------------------------------------------
def test_normalize_flat_fields():
    records = [{"name": "get_optin", "id": "c1", "ts": NOW.isoformat(), "in": {"contact_id": 42}}]
    mapping = TraceMapping(tool="name", call_id="id", observed_at="ts", args="in")
    result = normalize(records, mapping)
    assert result.skipped == []
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.tool == "get_optin"
    assert call.call_id == "c1"
    assert call.args == {"contact_id": 42}
    assert call.observed_at == NOW


def test_normalize_dotted_paths():
    records = [
        {
            "name": "get_optin",
            "id": "c1",
            "ts": NOW.isoformat(),
            "data": {"inputs": {"contact_id": 42}, "output": {"found": True, "value": True}},
        }
    ]
    mapping = TraceMapping(
        tool="name", call_id="id", observed_at="ts", args="data.inputs", result="data.output"
    )
    result = normalize(records, mapping)
    assert len(result.calls) == 1
    assert result.calls[0].args == {"contact_id": 42}
    assert result.calls[0].result == {"found": True, "value": True}


def test_normalize_skips_missing_field_not_silently():
    records = [
        {"name": "get_optin", "id": "c1", "ts": NOW.isoformat()},  # ok
        {"name": "get_optin", "ts": NOW.isoformat()},              # missing call_id
    ]
    mapping = TraceMapping(tool="name", call_id="id", observed_at="ts")
    result = normalize(records, mapping)
    assert len(result.calls) == 1
    assert len(result.skipped) == 1
    assert "missing call_id" in result.skipped[0]


def test_normalize_skips_unparseable_timestamp():
    records = [{"name": "get_optin", "id": "c1", "ts": "not-a-date"}]
    mapping = TraceMapping(tool="name", call_id="id", observed_at="ts")
    result = normalize(records, mapping)
    assert result.calls == []
    assert "unparseable observed_at" in result.skipped[0]


# --- simulate --------------------------------------------------------------
def _trace(contact: int, days_ago: int, found: bool) -> list[ToolCall]:
    result = {"found": found, "value": True} if found else {"found": False}
    return [
        ToolCall(
            tool="get_optin", args={"contact_id": contact}, result=result,
            call_id=f"c{contact}", observed_at=NOW - timedelta(days=days_ago),
        ),
        ToolCall(
            tool="send_marketing", args={"contact_id": contact},
            call_id=f"s{contact}", observed_at=NOW,
        ),
    ]


def _run(calls: list[ToolCall]):
    gate = Gate(PolicySet.from_dir("policies"))
    return simulate(
        calls, gate=gate, builder=_builder(),
        action_mapping={"send_*": ACTION}, now=NOW,
    )


def test_simulate_fresh_allows():
    reports = _run(_trace(42, days_ago=60, found=True))
    assert len(reports) == 1
    assert reports[0].effect.value == "allow"
    assert reports[0].executed is True


def test_simulate_stale_reviews():
    reports = _run(_trace(77, days_ago=425, found=True))
    assert reports[0].effect.value == "review"
    assert reports[0].executed is False


def test_simulate_missing_blocks():
    reports = _run(_trace(99, days_ago=0, found=False))
    assert reports[0].effect.value == "block"
    assert reports[0].executed is False


def test_simulate_scopes_evidence_per_turn():
    # A multi-subject trace: fresh 42 then missing 99. If evidence leaked across
    # turns, 99 would wrongly ALLOW on 42's opt-in. Turn-scoping keeps them apart.
    calls = _trace(42, 60, True) + _trace(99, 0, False)
    reports = _run(calls)
    assert [r.effect.value for r in reports] == ["allow", "block"]


def test_simulate_deterministic_in_order():
    calls = _trace(42, 60, True)
    first = _run(calls)
    again = _run(calls)
    assert [r.model_dump() for r in first] == [r.model_dump() for r in again]
