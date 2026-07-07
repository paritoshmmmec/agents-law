"""Telemetry tests: hygienic DecisionEvent + OTel span events, no sensitive leak.

The hygiene test is the load-bearing one: whatever we ship to an observability
backend must never carry the payload args, an evidence value/claim, or model text.
The OTel span test uses the in-memory SDK exporter, so no collector is needed; it
is skipped if the SDK isn't installed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from evidence_gate import (
    DecisionEvent,
    EvidenceItem,
    EvidenceManifest,
    EvidenceSource,
    Gate,
    NullSink,
    OTelSink,
    PolicySet,
    ProposedAction,
)
from evidence_gate.telemetry import DECISION_EVENT, PENDING_REVIEW_EVENT

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "marketing.send_sequence"

# A recognizable secret in every sensitive field, so the hygiene test can prove
# it appears nowhere in the emitted attributes.
SECRET = "SENSITIVE-DO-NOT-SHIP"


def _manifest(days_ago: int, found: bool = True) -> EvidenceManifest:
    items = []
    if found:
        items.append(
            EvidenceItem(
                id="optin-1",
                claim=f"opted in — {SECRET}",  # sensitive claim text
                key="marketing.opt_in",
                value=True,  # policy requires equals:true
                source=EvidenceSource.TOOL_RESULT,
                source_id=f"c1-{SECRET}",  # sensitive provenance handle
                observed_at=NOW - timedelta(days=days_ago),
                confidence=1.0,
                observed=True,
            )
        )
    return EvidenceManifest(items=items, compiled_at=NOW)


def _action() -> ProposedAction:
    return ProposedAction(
        action=ACTION,
        payload={"secret_arg": SECRET, "contact_id": 42},  # sensitive args
        actor="agent",
        request_id="req-1",
    )


# --- DecisionEvent hygiene -------------------------------------------------
def test_event_carries_keys_counts_not_values():
    gate = Gate(PolicySet.from_dir("policies"))
    decision = gate.check(_action(), _manifest(days_ago=60), now=NOW).decision
    event = DecisionEvent.from_decision(_action(), _manifest(60), decision)

    assert event.action == ACTION
    assert event.effect.value == "allow"
    assert event.evidence_keys == ["marketing.opt_in"]  # key, not value
    assert event.evidence_count == 1
    assert event.rule_ids  # some rule fired


def test_no_sensitive_value_in_attributes():
    gate = Gate(PolicySet.from_dir("policies"))
    decision = gate.check(_action(), _manifest(days_ago=60), now=NOW).decision
    event = DecisionEvent.from_decision(_action(), _manifest(60), decision)

    blob = json.dumps(event.attributes())
    assert SECRET not in blob  # no value, claim, or payload arg shipped
    assert "secret_arg" not in blob


def test_review_uses_pending_review_event_name():
    gate = Gate(PolicySet.from_dir("policies"))
    result = gate.check(_action(), _manifest(days_ago=425), now=NOW)  # stale -> REVIEW
    assert result.effect.value == "review"
    event = DecisionEvent.from_decision(
        _action(), _manifest(425), result.decision, review_ticket=result.review_ticket
    )
    assert event.name == PENDING_REVIEW_EVENT
    assert event.review_ticket == result.review_ticket


def test_allow_uses_decision_event_name():
    gate = Gate(PolicySet.from_dir("policies"))
    decision = gate.check(_action(), _manifest(days_ago=60), now=NOW).decision
    event = DecisionEvent.from_decision(_action(), _manifest(60), decision)
    assert event.name == DECISION_EVENT


# --- sink wiring -----------------------------------------------------------
class _SpySink:
    def __init__(self) -> None:
        self.events: list[DecisionEvent] = []

    def emit(self, event: DecisionEvent, *, emitted_at) -> None:
        self.events.append(event)


def test_gate_emits_one_event_per_check():
    spy = _SpySink()
    gate = Gate(PolicySet.from_dir("policies"), telemetry=spy)
    gate.check(_action(), _manifest(days_ago=60), now=NOW)
    gate.check(_action(), _manifest(days_ago=425), now=NOW)
    assert len(spy.events) == 2
    assert [e.effect.value for e in spy.events] == ["allow", "review"]


def test_missing_manifest_still_emits_block_event():
    spy = _SpySink()
    gate = Gate(PolicySet.from_dir("policies"), telemetry=spy)
    gate.check(_action(), None, now=NOW)  # structural BLOCK
    assert len(spy.events) == 1
    assert spy.events[0].effect.value == "block"


def test_default_gate_has_null_sink_and_does_not_raise():
    gate = Gate(PolicySet.from_dir("policies"))
    assert isinstance(gate.telemetry, NullSink)
    gate.check(_action(), _manifest(days_ago=60), now=NOW)  # no telemetry, no error


# --- real OTel span event --------------------------------------------------
def test_otel_span_event_emitted_and_hygienic():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    gate = Gate(PolicySet.from_dir("policies"), telemetry=OTelSink())
    with tracer.start_as_current_span("agent-turn"):
        gate.check(_action(), _manifest(days_ago=60), now=NOW)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    events = spans[0].events
    assert [e.name for e in events] == [DECISION_EVENT]
    attrs = dict(events[0].attributes)
    assert attrs["evidence_gate.action"] == ACTION
    assert attrs["evidence_gate.effect"] == "allow"
    assert SECRET not in json.dumps(attrs)  # hygiene holds through the SDK too


def test_otel_sink_noop_without_active_span():
    # No tracer/span active: OTelSink must be a silent no-op, not an error.
    gate = Gate(PolicySet.from_dir("policies"), telemetry=OTelSink())
    result = gate.check(_action(), _manifest(days_ago=60), now=NOW)
    assert result.effect.value == "allow"
