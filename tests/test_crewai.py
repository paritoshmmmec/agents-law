"""CrewAI adapter tests: a GatedTool blocks/allows a sensitive call in _run.

Guarded by importorskip so the suite still runs without the `crewai` extra. The
gated tools are invoked directly (as a CrewAI agent would via `.run()`), so no LLM
or crew execution is needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("crewai")

from crewai.tools import BaseTool  # noqa: E402

from evidence_gate import (  # noqa: E402
    EvidenceItem,
    EvidenceSource,
    Gate,
    GateSession,
    LocalGatePort,
    ManifestBuilder,
    PolicySet,
    gate_crew_tools,
)
from evidence_gate.errors import ClearanceDenied  # noqa: E402
from evidence_gate.trace import ToolCall  # noqa: E402

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "marketing.send_sequence"


def _optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    result = call.result
    if not result or not result.get("found"):
        return []
    observed_at = result.get("observed_at", call.observed_at)
    if isinstance(observed_at, str):
        observed_at = datetime.fromisoformat(observed_at)
    return [
        EvidenceItem(
            id=f"optin-{call.call_id}",
            claim="marketing opt-in",
            key="marketing.opt_in",
            value=result["value"],
            source=EvidenceSource.TOOL_RESULT,
            source_id=call.call_id,
            observed_at=observed_at,
            observed=True,
        )
    ]


def _builder() -> ManifestBuilder:
    return ManifestBuilder().register("get_optin", _optin_extractor)


class GetOptin(BaseTool):
    name: str = "get_optin"
    description: str = "Fetch a contact's marketing opt-in record."
    days_ago: int | None = 60

    def _run(self, **kwargs) -> dict:
        if self.days_ago is None:
            return {"found": False}
        return {
            "found": True,
            "value": True,
            "observed_at": (NOW - timedelta(days=self.days_ago)).isoformat(),
        }


class SendMarketing(BaseTool):
    name: str = "send_marketing"
    description: str = "Send a marketing sequence to a contact."
    sent: list = []

    def _run(self, **kwargs) -> str:
        self.sent.append(kwargs)
        return "sent"


def _session() -> GateSession:
    port = LocalGatePort(Gate(PolicySet.from_dir("policies")), _builder())
    return GateSession(port, action_mapping={"send_*": ACTION}, now=NOW)


def _wire(days_ago: int | None):
    """Wrap a fresh get_optin/send_marketing pair sharing one session."""
    send = SendMarketing(sent=[])
    tools = gate_crew_tools([GetOptin(days_ago=days_ago), send], _session())
    by_name = {t.name: t for t in tools}
    return by_name["get_optin"], by_name["send_marketing"], send


def test_fresh_optin_allows_send():
    get_optin, send_tool, raw_send = _wire(days_ago=60)
    get_optin.run(contact_id=42)  # records evidence
    assert send_tool.run(contact_id=42) == "sent"
    assert raw_send.sent == [{"contact_id": 42}]  # inner tool actually ran


def test_missing_optin_blocks_send():
    get_optin, send_tool, raw_send = _wire(days_ago=None)
    get_optin.run(contact_id=99)  # no record -> no evidence
    with pytest.raises(ClearanceDenied) as exc:
        send_tool.run(contact_id=99)
    assert exc.value.decision.effect.value == "block"
    assert raw_send.sent == []  # inner tool never ran


def test_stale_optin_reviews_and_blocks_send():
    get_optin, send_tool, raw_send = _wire(days_ago=425)
    get_optin.run(contact_id=77)
    with pytest.raises(ClearanceDenied) as exc:
        send_tool.run(contact_id=77)
    assert exc.value.decision.effect.value == "review"
    assert raw_send.sent == []


def test_evidence_tool_is_not_gated():
    # get_optin maps to no action, so it always runs and just records evidence.
    get_optin, _send, _raw = _wire(days_ago=60)
    out = get_optin.run(contact_id=42)
    assert out["found"] is True
