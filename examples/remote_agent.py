"""End-to-end: an agent driving the gate *over HTTP* (COMPARISON.md §6).

Starts the gate service (service.py) in a background thread, points a fail-closed
`RemoteGate` at it, and runs the marketing scenarios both ways:

  * declaring an evidence `manifest` (the agent self-reports its evidence), and
  * posting the observed `tool_calls` and letting the *server* reconstruct the
    manifest via a registered extractor (the agent can't fabricate lineage).

Both paths hit the same pure `gate.check()` and yield the same verdict for the
same facts. ALLOW/RESTRICT come back with a signed clearance token; the client
verifies it on receipt. Finally we point the client at a dead port to show the
fail-closed path — an unreachable gate never means "allow".

    uv run --extra service --extra client examples/remote_agent.py
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import uvicorn

from evidence_gate import (
    EvidenceItem,
    EvidenceManifest,
    Gate,
    PolicySet,
    ProposedAction,
    Signer,
    ToolCall,
    Verifier,
)
from evidence_gate.client import ClearanceDenied, GateUnreachable, RemoteGate
from evidence_gate.service import create_app
from evidence_gate.trace import ManifestBuilder

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "marketing.send_sequence"
KEY = b"demo-signing-key-not-for-prod"

# Ground truth, mirroring examples/llm_agent.py: 42 ALLOW, 77 REVIEW (stale), 99 BLOCK.
GROUND_TRUTH: dict[int, int | None] = {42: 60, 77: 425, 99: None}


def optin_evidence(contact: int) -> dict[str, Any] | None:
    days = GROUND_TRUTH[contact]
    if days is None:
        return None
    return {
        "id": f"e-{contact}",
        "claim": "contact opted in to marketing",
        "key": "marketing.opt_in",
        "value": True,
        "source": "tool_result",
        "source_id": f"crm:contact:{contact}",
        "observed_at": (NOW - timedelta(days=days)).isoformat(),
        "observed": True,
        "confidence": 1.0,
    }


def optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    """Server-side: turn a recorded get_optin call into an evidence item."""
    rec = call.result
    if not rec or not rec.get("found"):
        return []
    return [
        EvidenceItem(
            id=f"trace-{call.call_id}",
            claim="contact opted in to marketing",
            key="marketing.opt_in",
            value=rec["value"],
            source=rec["source"],
            source_id=rec["source_id"],
            observed_at=rec["observed_at"],
            observed=rec["observed"],
            confidence=rec["confidence"],
        )
    ]


def start_service(port: int) -> Gate:
    gate = Gate(PolicySet.from_dir("policies"), audit=None)
    builder = ManifestBuilder().register("get_optin", optin_extractor)
    app = create_app(gate, signer=Signer(KEY), builder=builder, token_ttl_seconds=300)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    # Wait for the socket to come up.
    for _ in range(50):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    return gate


def run_manifest_path(client: RemoteGate, contact: int) -> None:
    ev = optin_evidence(contact)
    manifest = EvidenceManifest(
        items=[EvidenceItem.model_validate(ev)] if ev else [], compiled_at=NOW
    )
    action = ProposedAction(
        action=ACTION, payload={"contact_id": contact}, actor="remote-agent",
        request_id=f"m-{contact}",
    )
    _run(client, action, manifest=manifest)


def run_tool_calls_path(client: RemoteGate, contact: int) -> None:
    ev = optin_evidence(contact)
    result = {"found": True, **{k: v for k, v in ev.items() if k != "id"}} if ev else {"found": False}
    calls = [ToolCall(tool="get_optin", args={"contact_id": contact}, result=result,
                      call_id=f"c-{contact}", observed_at=NOW)]
    action = ProposedAction(
        action=ACTION, payload={"contact_id": contact}, actor="remote-agent",
        request_id=f"t-{contact}",
    )
    _run(client, action, tool_calls=calls)


def _run(client: RemoteGate, action: ProposedAction, **evidence) -> None:
    try:
        result = client.check(action, now=NOW, **evidence)
    except ClearanceDenied as exc:
        print(f"    BLOCK  — {exc.reason}")
        return
    tok = "token✓" if result.claims else ("token(unverified)" if result.token else "no-token")
    if result.effect.value == "review":
        print(f"    REVIEW — ticket {result.review_ticket}")
    else:
        print(f"    {result.effect.value.upper():6} — {tok}")


def main() -> None:
    port = 8137
    print("=" * 70)
    print("Evidence Gate — remote agent over HTTP")
    print("=" * 70)
    start_service(port)
    client = RemoteGate(f"http://127.0.0.1:{port}", verifier=Verifier(KEY))

    for contact in (42, 77, 99):
        print(f"\n■ contact {contact}")
        print("  manifest path (agent-declared):")
        run_manifest_path(client, contact)
        print("  tool_calls path (server-reconstructed):")
        run_tool_calls_path(client, contact)

    print("\n■ fail-closed: pointing client at a dead port")
    dead = RemoteGate("http://127.0.0.1:59999", timeout=1.0)
    action = ProposedAction(action=ACTION, payload={"contact_id": 42}, actor="a", request_id="dead")
    try:
        dead.check(action, manifest=EvidenceManifest(items=[], compiled_at=NOW), now=NOW)
        print("    !! executed (BUG — should have failed closed)")
    except GateUnreachable as exc:
        print(f"    GateUnreachable (fail closed, tool NOT run): {str(exc)[:60]}…")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
