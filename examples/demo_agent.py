"""End-to-end demo: a (fake) agent proposing actions through the gate.

Run with:  uv run examples/demo_agent.py

The "agent" here is deliberately dumb — it just builds payloads and manifests.
The point is to watch the gate turn identical, well-formed tool calls into
different verdicts based purely on the *evidence* behind them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from evidence_gate import (
    ActionBlocked,
    Effect,
    EvidenceItem,
    EvidenceManifest,
    EvidenceSource,
    Gate,
    PolicySet,
    ProposedAction,
)

# A fixed "now" so the demo is reproducible (staleness depends on it).
NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def opt_in_item(
    days_ago: int,
    *,
    value: bool = True,
    source: EvidenceSource = EvidenceSource.TOOL_RESULT,
    observed: bool = True,
    item_id: str = "opt-in-1",
) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        claim=f"user marketing opt-in = {value}",
        key="marketing.opt_in",
        value=value,
        source=source,
        source_id="crm:contact/42",
        observed_at=NOW - timedelta(days=days_ago),
        observed=observed,
    )


def manifest(items: list[EvidenceItem]) -> EvidenceManifest:
    return EvidenceManifest(items=items, compiled_at=NOW)


SCENARIOS: list[tuple[str, EvidenceManifest | None]] = [
    # (label, manifest) — the marketing tripwire from DESIGN §10.
    ("happy path — fresh observed opt-in", manifest([opt_in_item(60)])),
    ("hallucinated request — no evidence", manifest([])),
    ("stale opt-in — 14 months old", manifest([opt_in_item(425)])),
    (
        "inferred, not observed",
        manifest([opt_in_item(60, source=EvidenceSource.INFERENCE, observed=False)]),
    ),
    (
        "conflicting sources",
        manifest(
            [
                opt_in_item(60, value=True, item_id="opt-in-crm"),
                opt_in_item(
                    30, value=False, source=EvidenceSource.RETRIEVAL, item_id="opt-in-doc"
                ),
            ]
        ),
    ),
    ("missing manifest entirely", None),
]


def run() -> None:
    gate = Gate(PolicySet.from_dir("policies"))

    print("=" * 70)
    print("Marketing tripwire: marketing.send_sequence")
    print("=" * 70)

    for label, m in SCENARIOS:
        action = ProposedAction(
            action="marketing.send_sequence",
            payload={"campaign": "summer-2026", "contact_id": 42},
            actor="marketing-agent",
            request_id=f"req-{label[:12]}",
        )
        result = gate.check(action, m, now=NOW)
        reasons = "; ".join(r.reason for r in result.decision.results if r.effect != Effect.ALLOW)
        verdict = result.effect.value.upper()
        extra = f"  [ticket {result.review_ticket}]" if result.review_ticket else ""
        print(f"\n• {label}")
        print(f"    -> {verdict}{extra}")
        if reasons:
            print(f"       {reasons}")

    print("\n" + "=" * 70)
    print(f"Audit: {len(gate.audit.records)} records, chain intact: {gate.audit.verify()}")
    print(f"Pending human reviews: {len(gate.review.pending())}")
    print("=" * 70)

    # Show a human resolving one of the parked reviews. The resolution is itself
    # audited (DESIGN §8) — resolve_review appends an approver-stamped record.
    pending = gate.review.pending()
    if pending:
        ticket = pending[0]
        resolved = gate.resolve_review(
            ticket.ticket_id, approver="ops@corp", effect=Effect.ALLOW, now=NOW
        )
        print(
            f"\nHuman resolved {resolved.ticket_id}: "
            f"{resolved.approver} -> {resolved.resolved_effect.value} "
            f"(audit now {len(gate.audit.records)} records, chain intact: {gate.audit.verify()})"
        )

    # Demonstrate that BLOCK stops execution while the agent loop survives.
    print("\nExecuting via @enforce decorator:")

    @gate.enforce(action="marketing.send_sequence")
    def send_sequence(payload: dict, effect: Effect) -> str:
        return f"email sent to contact {payload['contact_id']} (mode={effect.value})"

    try:
        out = send_sequence(
            {"contact_id": 42},
            manifest=manifest([opt_in_item(60)]),
            request_id="req-exec-ok",
            now=NOW,
        )
        print(f"  allow  -> {out}")
    except ActionBlocked as exc:
        print(f"  blocked-> {exc}")

    try:
        send_sequence(
            {"contact_id": 99},
            manifest=manifest([]),
            request_id="req-exec-block",
            now=NOW,
        )
    except ActionBlocked as exc:
        print(f"  block  -> {exc}  (agent loop continues)")


if __name__ == "__main__":
    run()
