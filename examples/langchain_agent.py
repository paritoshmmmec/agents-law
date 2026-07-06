"""LangChain adapter demo — gate a tool run via the callback handler (COMPARISON.md §6 #5).

`EvidenceGateCallbackHandler` sits on LangChain's tool-call callbacks: it collects
the evidence tools an agent runs and, when the agent is about to run the *sensitive*
`send_marketing` tool, routes that call through the gate — blocking or reviewing
before it executes. The same handler works against an in-process `Gate`
(`LocalGatePort`) or the fail-closed remote client (`RemoteGatePort`).

To keep the demo dependency-light we don't spin up a full LangChain agent; we invoke
the handler's callbacks (`on_tool_start` / `on_tool_end`) in the exact order
LangChain would — get_optin, then send_marketing — for contacts 42 / 77 / 99.

    uv run --extra langchain examples/langchain_agent.py
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import uvicorn

from evidence_gate import (
    ClearanceDenied,
    EvidenceItem,
    EvidenceSource,
    Gate,
    ManifestBuilder,
    PolicySet,
    RemoteGate,
    Signer,
    create_app,
)
from evidence_gate.integrations.langchain import (
    EvidenceGateCallbackHandler,
    LocalGatePort,
    RemoteGatePort,
)
from evidence_gate.trace import ToolCall

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "marketing.send_sequence"

# Same ground truth as llm_agent.py: 42 fresh -> ALLOW, 77 stale -> REVIEW, 99 none -> BLOCK.
GROUND_TRUTH = {
    42: {"found": True, "value": True, "days": 60},
    77: {"found": True, "value": True, "days": 425},
    99: {"found": False},
}


def optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    result = call.result
    if not result or not result.get("found"):
        return []
    # Prefer the fact's own timestamp (when the opt-in happened, per the CRM) over
    # when the tool ran — staleness is about the fact, not the fetch.
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
    return ManifestBuilder().register("get_optin", optin_extractor)


def _optin_result(contact: int) -> dict:
    rec = dict(GROUND_TRUTH[contact])
    if rec.get("found"):
        rec["observed_at"] = (NOW - timedelta(days=rec.pop("days"))).isoformat()
    return rec


def drive(handler: EvidenceGateCallbackHandler, contact: int) -> str:
    """Replay one agent turn through the handler's callbacks, as LangChain would."""
    # 1. Evidence tool: get_optin runs, result collected.
    ev_run = f"getoptin-{contact}"
    handler.on_tool_start({"name": "get_optin"}, "", run_id=ev_run, inputs={"contact_id": contact})
    handler.on_tool_end(json.dumps(_optin_result(contact)), run_id=ev_run)

    # 2. Sensitive tool: send_marketing is gated in on_tool_start.
    send_run = f"send-{contact}"
    try:
        handler.on_tool_start(
            {"name": "send_marketing"}, "", run_id=send_run,
            inputs={"contact_id": contact, "campaign": "summer-2026"},
        )
    except ClearanceDenied as denied:
        # The handler raises on both BLOCK and REVIEW; the decision effect tells
        # them apart. Either way the sensitive tool never ran.
        return f"{denied.decision.effect.value.upper()} — {denied.reason}"
    # Reached only on ALLOW/RESTRICT — LangChain would now execute the tool.
    handler.on_tool_end("sent", run_id=send_run)
    return "ALLOW — tool executed"


def run_section(title: str, make_handler) -> None:
    print(f"\n{title}")
    for contact in (42, 77, 99):
        outcome = make_handler_and_drive(make_handler, contact)
        print(f"  contact {contact}: {outcome}")


def make_handler_and_drive(make_handler, contact: int) -> str:
    handler = make_handler()  # fresh handler per turn = fresh evidence buffer
    return drive(handler, contact)


def main() -> None:
    print("=" * 70)
    print("LangChain adapter — the same handler, local and over the wire")
    print("=" * 70)

    # --- Local: in-process Gate ------------------------------------------------
    gate = Gate(PolicySet.from_dir("policies"))
    run_section(
        "LocalGatePort (in-process Gate):",
        lambda: EvidenceGateCallbackHandler(
            LocalGatePort(gate, _builder()),
            action_mapping={"send_*": ACTION},
            now=NOW,
        ),
    )

    # --- Remote: fail-closed client against a live service ---------------------
    signer = Signer(b"demo-key")
    app = create_app(Gate(PolicySet.from_dir("policies")), signer=signer, builder=_builder())
    config = uvicorn.Config(app, host="127.0.0.1", port=8137, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    try:
        remote = RemoteGate("http://127.0.0.1:8137")
        run_section(
            "RemoteGatePort (fail-closed client → live service):",
            lambda: EvidenceGateCallbackHandler(
                RemoteGatePort(remote),
                action_mapping={"send_*": ACTION},
                now=NOW,
            ),
        )
    finally:
        server.should_exit = True
        thread.join(timeout=2)

    print("\n" + "=" * 70)
    print("Same handler, same verdicts, whether the gate is in-process or remote.")
    print("=" * 70)


if __name__ == "__main__":
    main()
