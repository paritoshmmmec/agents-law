"""Trace-to-Gate: replay a recorded agent trace through the gate (COMPARISON.md §6 #4).

The onboarding demo. Instead of wiring the gate into a live agent, take the log of
tool calls an agent *already* made and ask the gate what it *would have decided*.
Here the trace is a canned OpenAI-style log for three contacts — the same 42 / 77 /
99 ground truth `llm_agent.py` drives live — and the replay reaches the identical
ALLOW / REVIEW / BLOCK verdicts, with nothing wired into the agent itself.

    uv run examples/trace_replay.py
"""

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


def optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    """Map a get_optin tool result into an opt-in evidence item (same as test_trace)."""
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


def _record(tool: str, call_id: str, days_ago: int, result: dict | None, args: dict) -> dict:
    """One trace record in a home-grown/exported shape (nested under `data`)."""
    return {
        "name": tool,
        "id": call_id,
        "ts": (NOW - timedelta(days=days_ago)).isoformat(),
        "data": {"inputs": args, "output": result},
    }


# A canned trace: for each contact the agent fetched opt-in, then tried to send.
#   42 -> fresh opt-in (60d)      -> ALLOW
#   77 -> stale opt-in (425d)     -> REVIEW
#   99 -> no record at all        -> BLOCK (no evidence)
TRACE: list[dict] = [
    _record("get_optin", "c42", 60, {"found": True, "value": True}, {"contact_id": 42}),
    _record("send_marketing", "s42", 0, None, {"contact_id": 42, "campaign": "summer-2026"}),
    _record("get_optin", "c77", 425, {"found": True, "value": True}, {"contact_id": 77}),
    _record("send_marketing", "s77", 0, None, {"contact_id": 77, "campaign": "summer-2026"}),
    _record("get_optin", "c99", 0, {"found": False}, {"contact_id": 99}),
    _record("send_marketing", "s99", 0, None, {"contact_id": 99, "campaign": "summer-2026"}),
]

MAPPING = TraceMapping(
    tool="name",
    call_id="id",
    observed_at="ts",
    args="data.inputs",
    result="data.output",
)


def main() -> None:
    print("=" * 70)
    print("Trace-to-Gate — replay a recorded trace through the gate")
    print("=" * 70)

    norm = normalize(TRACE, MAPPING)
    print(f"\nNormalized {len(norm.calls)} tool call(s); skipped {len(norm.skipped)}.")
    for reason in norm.skipped:
        print(f"  ! {reason}")

    gate = Gate(PolicySet.from_dir("policies"))
    builder = ManifestBuilder().register("get_optin", optin_extractor)

    reports = simulate(
        norm.calls,
        gate=gate,
        builder=builder,
        action_mapping={"send_*": ACTION},
        now=NOW,
    )

    print(f"\n{len(reports)} sensitive call(s) replayed through the gate:\n")
    for r in reports:
        line = f"  {r.request_id:>4}  {r.effect.value.upper():<8} executed={r.executed}"
        if r.reasons:
            line += f"  — {'; '.join(r.reasons)}"
        print(line)

    print("\n" + "=" * 70)
    print(f"Audit: {len(gate.audit.records)} records, chain intact: {gate.audit.verify()}")
    print("The gate reached these verdicts from the log alone — nothing wired live.")
    print("=" * 70)


if __name__ == "__main__":
    main()
