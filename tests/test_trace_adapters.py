"""Trace-to-Gate tests: generic normalize + simulate replay reaches the same verdicts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from evidence_gate import (
    LANGFUSE,
    LANGSMITH,
    OPENAI,
    EvidenceItem,
    EvidenceSource,
    Gate,
    ManifestBuilder,
    PolicySet,
    TraceMapping,
    coverage,
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


# --- vendor presets --------------------------------------------------------
def test_langsmith_preset():
    # A LangSmith Run (run_type="tool"): dict inputs/outputs, ISO8601 start_time.
    run = {
        "id": "run-1",
        "name": "get_optin",
        "run_type": "tool",
        "start_time": NOW.isoformat(),
        "inputs": {"contact_id": 42},
        "outputs": {"found": True, "value": True},
    }
    result = normalize([run], LANGSMITH)
    assert result.skipped == []
    call = result.calls[0]
    assert call.tool == "get_optin"
    assert call.call_id == "run-1"
    assert call.args == {"contact_id": 42}
    assert call.result == {"found": True, "value": True}
    assert call.observed_at == NOW


def test_langfuse_preset_decodes_json_string_io():
    # Langfuse read API returns input/output as JSON *strings* by default.
    obs = {
        "id": "obs-1",
        "type": "SPAN",
        "name": "get_optin",
        "startTime": NOW.isoformat(),
        "input": json.dumps({"contact_id": 42}),
        "output": json.dumps({"found": True, "value": True}),
    }
    result = normalize([obs], LANGFUSE)
    assert result.skipped == []
    call = result.calls[0]
    assert call.args == {"contact_id": 42}  # decoded from the JSON string
    assert call.result == {"found": True, "value": True}


def test_openai_preset_epoch_and_json_arguments():
    # One tool_call flattened with the completion's `created` (epoch seconds).
    tool_call = {
        "id": "call-1",
        "created": int(NOW.timestamp()),
        "function": {"name": "get_optin", "arguments": json.dumps({"contact_id": 42})},
    }
    result = normalize([tool_call], OPENAI)
    assert result.skipped == []
    call = result.calls[0]
    assert call.tool == "get_optin"
    assert call.call_id == "call-1"
    assert call.args == {"contact_id": 42}  # decoded from the JSON string
    assert call.observed_at == NOW  # epoch seconds read as UTC
    assert call.result is None  # result arrives in a later role="tool" message


def test_malformed_json_arguments_contribute_no_args_not_crash():
    tool_call = {
        "id": "call-2",
        "created": int(NOW.timestamp()),
        "function": {"name": "get_optin", "arguments": "{not valid json"},
    }
    result = normalize([tool_call], OPENAI)
    assert result.calls[0].args == {}  # opt-in evidence: never invented


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


# --- coverage --------------------------------------------------------------
def test_coverage_classifies_each_tool():
    # get_optin -> extractor (recognized evidence); send_marketing -> action (gated).
    calls = _trace(42, 60, True)
    cov = coverage(calls, action_mapping={"send_*": ACTION}, builder=_builder())
    assert cov.gated == ["send_marketing"]
    assert cov.recognized_evidence == ["get_optin"]
    assert cov.unclassified == []
    assert cov.has_residual_risk is False
    assert cov.call_counts == {"get_optin": 1, "send_marketing": 1}


def test_coverage_surfaces_unclassified_tool():
    # An unwrapped, unrecognized tool is residual risk — named, never silently dropped.
    calls = _trace(42, 60, True) + [
        ToolCall(tool="wire_transfer", args={"amount": 9000}, call_id="w1", observed_at=NOW),
        ToolCall(tool="wire_transfer", args={"amount": 10}, call_id="w2", observed_at=NOW),
    ]
    cov = coverage(calls, action_mapping={"send_*": ACTION}, builder=_builder())
    assert cov.unclassified == ["wire_transfer"]
    assert cov.has_residual_risk is True
    assert cov.call_counts["wire_transfer"] == 2


def test_coverage_without_builder_treats_evidence_tools_as_residual():
    # No extractors registered: only gated tools are recognized, the rest is residual.
    calls = _trace(42, 60, True)
    cov = coverage(calls, action_mapping={"send_*": ACTION})
    assert cov.gated == ["send_marketing"]
    assert cov.recognized_evidence == []
    assert cov.unclassified == ["get_optin"]
