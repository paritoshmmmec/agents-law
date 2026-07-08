"""Service tests: HTTP verdicts, both manifest paths, tokens, audit endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from datetime import timedelta

from evidence_gate import Gate, ManifestBuilder, PolicySet, Signer
from evidence_gate.schemas import EvidenceItem
from evidence_gate.service import create_app
from evidence_gate.trace import ToolCall

from tests.conftest import NOW, manifest, opt_in

ACTION = "marketing.send_sequence"
KEY = b"service-test-key"


def _optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    rec = call.result
    if not rec or not rec.get("found"):
        return []
    return [
        EvidenceItem(
            id=f"t-{call.call_id}", claim="opt-in", key="marketing.opt_in",
            value=rec["value"], source=rec["source"], source_id="crm",
            observed_at=rec["observed_at"], observed=True, confidence=1.0,
        )
    ]


@pytest.fixture
def client() -> TestClient:
    gate = Gate(PolicySet.from_dir("policies"))
    builder = ManifestBuilder().register("get_optin", _optin_extractor)
    return TestClient(create_app(gate, signer=Signer(KEY), builder=builder))


def _action(request_id: str = "r1") -> dict:
    return {"action": ACTION, "payload": {"contact": 1}, "actor": "agent", "request_id": request_id}


def _post(client, *, request_id="r1", manifest_obj=None, tool_calls=None):
    body = {"action": _action(request_id)}
    if manifest_obj is not None:
        body["manifest"] = manifest_obj.model_dump(mode="json")
    if tool_calls is not None:
        body["tool_calls"] = [tc.model_dump(mode="json") for tc in tool_calls]
    return client.post("/v1/check", json=body)


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_allow_manifest_path_carries_token(client):
    r = _post(client, manifest_obj=manifest(opt_in(60))).json()
    assert r["effect"] == "allow"
    assert r["executed"] is True
    assert r["token"]  # ALLOW carries a clearance token


def test_review_and_block_carry_no_token(client):
    review = _post(client, manifest_obj=manifest(opt_in(425))).json()
    assert review["effect"] == "review"
    assert review["token"] is None
    assert review["review_ticket"] is not None

    block = _post(client, manifest_obj=manifest()).json()
    assert block["effect"] == "block"
    assert block["token"] is None


def test_tool_calls_path_reconstructs_manifest(client):
    # A get_optin call the server turns into evidence -> same ALLOW as manifest path.
    call = ToolCall(
        tool="get_optin", args={"contact": 1},
        result={"found": True, "value": True, "source": "tool_result",
                "observed_at": (NOW.replace(microsecond=0)).isoformat()},
        call_id="c1", observed_at=NOW,
    )
    r = _post(client, tool_calls=[call]).json()
    assert r["effect"] == "allow"
    assert r["token"]


def test_both_paths_agree_for_same_facts(client):
    via_manifest = _post(client, request_id="m", manifest_obj=manifest(opt_in(60))).json()
    call = ToolCall(
        tool="get_optin", args={},
        result={"found": True, "value": True, "source": "tool_result",
                "observed_at": (NOW - timedelta(days=60)).isoformat()},
        call_id="c2", observed_at=NOW,
    )
    via_calls = _post(client, request_id="t", tool_calls=[call]).json()
    assert via_manifest["effect"] == via_calls["effect"] == "allow"


def test_missing_both_evidence_blocks_and_is_audited(client):
    # Neither manifest nor tool_calls -> structural BLOCK, still recorded.
    r = client.post("/v1/check", json={"action": _action("bare")}).json()
    assert r["effect"] == "block"
    audit = client.get("/v1/audit").json()
    assert any(rec["request_id"] == "bare" for rec in audit)


def test_supplying_both_is_rejected(client):
    body = {
        "action": _action(),
        "manifest": manifest(opt_in(60)).model_dump(mode="json"),
        "tool_calls": [],
    }
    assert client.post("/v1/check", json=body).status_code == 422


def test_audit_chain_intact_after_checks(client):
    _post(client, request_id="a", manifest_obj=manifest(opt_in(60)))
    _post(client, request_id="b", manifest_obj=manifest(opt_in(425)))
    assert client.get("/v1/audit/verify").json() == {"intact": True}


def test_review_resolution_over_http(client):
    review = _post(client, request_id="rev", manifest_obj=manifest(opt_in(425))).json()
    ticket_id = review["review_ticket"]
    r = client.post(f"/v1/review/{ticket_id}/resolve", json={"approver": "ops", "effect": "allow"})
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] is True and body["approver"] == "ops"
    assert client.get("/v1/review/pending").json() == []
