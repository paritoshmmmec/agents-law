"""RESTRICT in action: a refund tool that degrades an over-ceiling payload.

Run with:  uv run examples/refund_agent.py

`billing.issue_refund` (policies/refund.yaml) uses the cross-key `compare`
primitive two ways:

  * refund.amount <= order.total   -> BLOCK if violated (can't refund more than
    was ever charged), and
  * refund.amount <= 5000          -> RESTRICT if violated (over the auto-approve
    ceiling: execute a *capped partial* instead of the full amount).

The tool below is wrapped with `@gate.enforce`. On RESTRICT the gate still lets
it run but tells it `effect=RESTRICT`, so the tool degrades its own payload —
the concrete RESTRICT execution path the earlier prototype only stubbed.
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

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "billing.issue_refund"
AUTO_APPROVE_CEILING = 5000  # keep in step with refund.yaml


def _tool(key: str, value: object, item_id: str, days_ago: int = 3) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        claim=f"{key} = {value}",
        key=key,
        value=value,
        source=EvidenceSource.TOOL_RESULT,
        source_id="billing:order/1001",
        observed_at=NOW - timedelta(days=days_ago),
        observed=True,
    )


def refund_evidence(order_total: int, refund_amount: int) -> EvidenceManifest:
    """The evidence an honest agent would gather before proposing a refund."""
    return EvidenceManifest(
        items=[
            _tool("order.id", "order/1001", "ord"),
            _tool("order.paid", True, "paid"),
            _tool("order.total", order_total, "total"),
            _tool("refund.amount", refund_amount, "amt"),
        ],
        compiled_at=NOW,
    )


def run() -> None:
    gate = Gate(PolicySet.from_dir("policies"))

    @gate.enforce(action=ACTION)
    def issue_refund(payload: dict, effect: Effect) -> dict:
        """Issue the refund. On RESTRICT, cap it to the auto-approve ceiling."""
        amount = payload["refund_amount"]
        if effect is Effect.RESTRICT:
            amount = min(amount, AUTO_APPROVE_CEILING)
        return {"refunded": amount, "mode": effect.value}

    print("=" * 70)
    print("Refund tripwire: billing.issue_refund  (cross-key compare + RESTRICT)")
    print("=" * 70)

    scenarios = [
        ("within ceiling", 8000, 4000),   # ALLOW  — full refund
        ("over ceiling", 8000, 6500),     # RESTRICT — capped to 5000
        ("exceeds order total", 8000, 9000),  # BLOCK — impossible refund
    ]

    for label, order_total, refund_amount in scenarios:
        payload = {"order_id": "order/1001", "refund_amount": refund_amount}
        manifest = refund_evidence(order_total, refund_amount)
        print(f"\n• {label}: ask {refund_amount} on a {order_total} order")
        try:
            out = issue_refund(
                payload,
                manifest=manifest,
                request_id=f"req-{label[:10]}",
                now=NOW,
            )
            if isinstance(out, dict):
                degraded = " (degraded)" if out["refunded"] != refund_amount else ""
                print(f"    -> {out['mode'].upper()}: refunded {out['refunded']}{degraded}")
            else:  # GateResult (REVIEW): did not execute
                print(f"    -> {out.effect.value.upper()} [ticket {out.review_ticket}]")
        except ActionBlocked as exc:
            print(f"    -> BLOCK: {exc}")

    print("\n" + "=" * 70)
    print(f"Audit: {len(gate.audit.records)} records, chain intact: {gate.audit.verify()}")
    print("=" * 70)


if __name__ == "__main__":
    run()
