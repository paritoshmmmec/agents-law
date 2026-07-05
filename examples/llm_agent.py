"""A *real* LLM agent driving the Evidence Gate.

Unlike `demo_agent.py` (which hand-builds manifests), here an actual model
decides what to do. It is given two kinds of tools:

  * evidence-gathering tools (`get_optin`) that return ground-truth facts, and
  * a sensitive tool (`send_marketing`) it may only call *with an Evidence
    Manifest it declares itself*.

The sensitive tool handler routes that declared manifest through
`gate.check(...)`. The model never decides whether it is ready to act — the gate
does, deterministically. This is the DESIGN thesis exercised against a live model.

Run against a real model (needs OPENROUTER_API_KEY in .env):

    uv run examples/llm_agent.py

Run offline with a scripted stand-in model (no API, no quota, same gate path):

    uv run examples/llm_agent.py --mock

Point at any OpenAI-compatible endpoint via env:
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
    (defaults target OpenRouter's z-ai/glm-4.7)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from evidence_gate import (
    Effect,
    EvidenceItem,
    EvidenceManifest,
    Gate,
    PolicySet,
    ProposedAction,
)

# Fixed clock so staleness (and thus the gate's verdicts) are reproducible.
NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)

ACTION = "marketing.send_sequence"


# --------------------------------------------------------------------------
# Ground truth. The "CRM" the evidence tool reads from. Each contact is a
# scenario that should drive the gate to a different verdict.
# --------------------------------------------------------------------------
def _optin_record(days_ago: int, **over: Any) -> dict[str, Any]:
    rec = {
        "key": "marketing.opt_in",
        "value": True,
        "source": "tool_result",
        "source_id": "crm:contact",
        "observed_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "observed": True,
        "confidence": 1.0,
    }
    rec.update(over)
    return rec


GROUND_TRUTH: dict[int, dict[str, Any] | None] = {
    42: _optin_record(60),                       # fresh, observed  -> ALLOW
    77: _optin_record(425),                      # 14 months old    -> REVIEW (stale)
    99: None,                                     # no record at all -> BLOCK (missing)
}


# --------------------------------------------------------------------------
# Tools exposed to the model (OpenAI function-calling schema).
# --------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_optin",
            "description": (
                "Look up a contact's marketing opt-in record in the CRM. Returns "
                "the evidence object to attach to send_marketing, or {found:false}."
            ),
            "parameters": {
                "type": "object",
                "properties": {"contact_id": {"type": "integer"}},
                "required": ["contact_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_marketing",
            "description": (
                "Send the marketing sequence to a contact. You MUST pass `evidence`: "
                "the list of fact objects (as returned by get_optin) that justify "
                "sending. The Evidence Gate will judge the evidence and may block or "
                "route the send for review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer"},
                    "evidence": {
                        "type": "array",
                        "description": "Fact objects gathered from tools.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["contact_id", "evidence"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are a marketing agent. To email a contact you must FIRST call get_optin "
    "to fetch their opt-in evidence, THEN call send_marketing passing that exact "
    "evidence object in the `evidence` array. Never invent evidence; only pass what "
    "a tool returned. If get_optin reports no record, still call send_marketing with "
    "an empty evidence array so the gate can record the attempt."
)


# --------------------------------------------------------------------------
# Tool execution — where the gate lives.
# --------------------------------------------------------------------------
def _to_manifest(evidence: list[dict[str, Any]]) -> EvidenceManifest:
    """Turn the model's declared evidence list into a validated manifest."""
    items = []
    for i, e in enumerate(evidence):
        items.append(
            EvidenceItem(
                id=e.get("id", f"decl-{i}"),
                claim=e.get("claim", str(e.get("key", "?"))),
                key=e["key"],
                value=e["value"],
                source=e["source"],
                source_id=e.get("source_id", "unknown"),
                observed_at=e["observed_at"],
                observed=e.get("observed", True),
                confidence=e.get("confidence", 1.0),
            )
        )
    return EvidenceManifest(items=items, compiled_at=NOW)


def execute_tool(name: str, args: dict[str, Any], gate: Gate) -> str:
    """Run one tool call and return a JSON string result for the model."""
    if name == "get_optin":
        rec = GROUND_TRUTH.get(int(args["contact_id"]))
        if rec is None:
            return json.dumps({"found": False})
        return json.dumps({"found": True, **rec})

    if name == "send_marketing":
        contact = int(args["contact_id"])
        manifest = _to_manifest(args.get("evidence", []))
        action = ProposedAction(
            action=ACTION,
            payload={"contact_id": contact, "campaign": "summer-2026"},
            actor="glm-marketing-agent",
            request_id=f"req-{contact}",
        )
        result = gate.check(action, manifest, now=NOW)
        reasons = [r.reason for r in result.decision.results if r.effect != Effect.ALLOW]
        return json.dumps(
            {
                "gate_decision": result.effect.value,
                "executed": result.allowed,
                "review_ticket": result.review_ticket,
                "reasons": reasons,
            }
        )

    return json.dumps({"error": f"unknown tool {name}"})


# --------------------------------------------------------------------------
# Live model loop (OpenAI-compatible endpoint).
# --------------------------------------------------------------------------
def run_live(contact_id: int, gate: Gate) -> None:
    from openai import OpenAI

    base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    model = os.environ.get("LLM_MODEL", "z-ai/glm-4.7")
    if not api_key:
        sys.exit("No API key. Set OPENROUTER_API_KEY (or LLM_API_KEY) in .env")

    client = OpenAI(base_url=base_url, api_key=api_key)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Send the summer-2026 campaign to contact {contact_id}."},
    ]

    for _ in range(6):  # bounded tool loop
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto",
            temperature=1, max_tokens=1024,
        )
        msg = resp.choices[0].message
        assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant)

        if not msg.tool_calls:
            print(f"  model: {msg.content}")
            return

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            out = execute_tool(tc.function.name, args, gate)
            print(f"  → {tc.function.name}({args}) => {out}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

    print("  (tool loop cap reached)")


# --------------------------------------------------------------------------
# Mock model loop — a scripted stand-in that exercises the identical gate path
# offline. It behaves like an honest model: fetch, then send with what it got.
# --------------------------------------------------------------------------
def run_mock(contact_id: int, gate: Gate) -> None:
    optin = json.loads(execute_tool("get_optin", {"contact_id": contact_id}, gate))
    print(f"  → get_optin({{'contact_id': {contact_id}}}) => {json.dumps(optin)}")

    evidence: list[dict[str, Any]] = []
    if optin.get("found"):
        evidence = [{k: v for k, v in optin.items() if k != "found"}]

    out = execute_tool(
        "send_marketing", {"contact_id": contact_id, "evidence": evidence}, gate
    )
    print(f"  → send_marketing(contact={contact_id}, evidence={len(evidence)} item(s)) => {out}")


# --------------------------------------------------------------------------
def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Real LLM agent driving the Evidence Gate.")
    parser.add_argument("--mock", action="store_true", help="use a scripted stand-in model (no API)")
    parser.add_argument(
        "--contacts", type=int, nargs="*", default=[42, 77, 99],
        help="contact ids to run (42=allow, 77=review, 99=block)",
    )
    args = parser.parse_args()

    gate = Gate(PolicySet.from_dir("policies"))
    runner = run_mock if args.mock else run_live
    mode = "MOCK (scripted)" if args.mock else f"LIVE ({os.environ.get('LLM_MODEL', 'z-ai/glm-4.7')})"

    print("=" * 70)
    print(f"Evidence Gate — real agent loop  [{mode}]")
    print("=" * 70)
    for cid in args.contacts:
        print(f"\n■ contact {cid}")
        try:
            runner(cid, gate)
        except Exception as exc:  # keep the loop alive across a failing scenario
            print(f"  !! error: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 70)
    print(f"Audit: {len(gate.audit.records)} records, chain intact: {gate.audit.verify()}")
    print(f"Pending human reviews: {len(gate.review.pending())}")
    print("=" * 70)


if __name__ == "__main__":
    main()
