"""LangChain adapter tests: GatePort parity, handler gating, fail-closed.

Guarded by importorskip so the suite still runs without the `langchain` extra.
The handler's callbacks are invoked directly (as LangChain would), so no real
agent or network socket is needed — RemoteGatePort talks to a FastAPI TestClient.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("langchain_core")

from evidence_gate import (  # noqa: E402
    EvidenceItem,
    EvidenceSource,
    Gate,
    ManifestBuilder,
    PolicySet,
    RemoteGate,
    Signer,
    create_app,
)
from evidence_gate.client import ClearanceDenied, GateUnreachable  # noqa: E402
from evidence_gate.integrations.langchain import (  # noqa: E402
    EvidenceGateCallbackHandler,
    LocalGatePort,
    RemoteGatePort,
)
from evidence_gate.trace import ToolCall  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "marketing.send_sequence"
KEY = b"test-key"


def _optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    result = call.result
    if not result or not result.get("found"):
        return []
    observed_at = result.get("observed_at", call.observed_at)
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


def _optin_result(days_ago: int | None) -> dict:
    if days_ago is None:
        return {"found": False}
    return {"found": True, "value": True, "observed_at": (NOW - timedelta(days=days_ago)).isoformat()}


def _drive(handler: EvidenceGateCallbackHandler, contact: int, days_ago: int | None):
    """Replay get_optin -> send_marketing through the handler; return the send outcome."""
    ev = f"getoptin-{contact}"
    handler.on_tool_start({"name": "get_optin"}, "", run_id=ev, inputs={"contact_id": contact})
    handler.on_tool_end(json.dumps(_optin_result(days_ago)), run_id=ev)

    send = f"send-{contact}"
    handler.on_tool_start(
        {"name": "send_marketing"}, "", run_id=send, inputs={"contact_id": contact}
    )
    return send  # reached only when the send was allowed


# --- port parity -----------------------------------------------------------
def _local_port() -> LocalGatePort:
    return LocalGatePort(Gate(PolicySet.from_dir("policies")), _builder())


def _remote_port() -> RemoteGatePort:
    app = create_app(Gate(PolicySet.from_dir("policies")), signer=Signer(KEY), builder=_builder())
    remote = RemoteGate(transport=TestClient(app), verifier=None)
    return RemoteGatePort(remote)


@pytest.mark.parametrize("make_port", [_local_port, _remote_port])
def test_port_parity_allow(make_port):
    handler = EvidenceGateCallbackHandler(make_port(), action_mapping={"send_*": ACTION}, now=NOW)
    send = _drive(handler, 42, days_ago=60)  # fresh -> ALLOW, no raise
    handler.on_tool_end("sent", run_id=send)


@pytest.mark.parametrize("make_port", [_local_port, _remote_port])
def test_port_parity_block(make_port):
    handler = EvidenceGateCallbackHandler(make_port(), action_mapping={"send_*": ACTION}, now=NOW)
    with pytest.raises(ClearanceDenied) as exc:
        _drive(handler, 99, days_ago=None)  # missing -> BLOCK
    assert exc.value.decision.effect.value == "block"


@pytest.mark.parametrize("make_port", [_local_port, _remote_port])
def test_port_parity_review(make_port):
    handler = EvidenceGateCallbackHandler(make_port(), action_mapping={"send_*": ACTION}, now=NOW)
    with pytest.raises(ClearanceDenied) as exc:
        _drive(handler, 77, days_ago=425)  # stale -> REVIEW (handler raises, tool never runs)
    assert exc.value.decision.effect.value == "review"


# --- handler mechanics -----------------------------------------------------
def test_evidence_collected_on_tool_end():
    handler = EvidenceGateCallbackHandler(_local_port(), action_mapping={"send_*": ACTION}, now=NOW)
    handler.on_tool_start({"name": "get_optin"}, "", run_id="e1", inputs={"contact_id": 42})
    assert handler.evidence == []  # not yet finalized
    handler.on_tool_end(json.dumps(_optin_result(60)), run_id="e1")
    assert len(handler.evidence) == 1
    assert handler.evidence[0].tool == "get_optin"
    assert handler.evidence[0].result["found"] is True


def test_sensitive_tool_never_runs_when_blocked():
    handler = EvidenceGateCallbackHandler(_local_port(), action_mapping={"send_*": ACTION}, now=NOW)
    ran = {"send": False}

    # Simulate the tool body: it should never be invoked, because on_tool_start raises first.
    with pytest.raises(ClearanceDenied):
        handler.on_tool_start({"name": "send_marketing"}, "", run_id="s", inputs={"contact_id": 99})
        ran["send"] = True  # unreachable
    assert ran["send"] is False


def test_fail_closed_on_unreachable_gate():
    # RemoteGate at a dead port -> GateUnreachable from on_tool_start, tool never runs.
    remote = RemoteGate("http://127.0.0.1:1", timeout=0.2)
    handler = EvidenceGateCallbackHandler(
        RemoteGatePort(remote), action_mapping={"send_*": ACTION}, now=NOW
    )
    with pytest.raises(GateUnreachable):
        handler.on_tool_start({"name": "send_marketing"}, "", run_id="s", inputs={"contact_id": 42})
