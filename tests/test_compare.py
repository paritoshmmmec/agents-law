"""Cross-key `compare` primitive: thresholds, key-vs-key, RESTRICT degradation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from evidence_gate import (
    Effect,
    EvidenceItem,
    EvidenceManifest,
    EvidenceSource,
    Gate,
    PolicySet,
)
from evidence_gate.engine import evaluate
from evidence_gate.policy import Comparison, Rule

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "billing.issue_refund"


def _tool(key, value, item_id, days_ago=3):
    return EvidenceItem(
        id=item_id,
        claim=f"{key}={value}",
        key=key,
        value=value,
        source=EvidenceSource.TOOL_RESULT,
        source_id="billing:1001",
        observed_at=NOW - timedelta(days=days_ago),
        observed=True,
    )


def refund_manifest(order_total, refund_amount):
    return EvidenceManifest(
        items=[
            _tool("order.id", "order/1001", "ord"),
            _tool("order.paid", True, "paid"),
            _tool("order.total", order_total, "total"),
            _tool("refund.amount", refund_amount, "amt"),
        ],
        compiled_at=NOW,
    )


@pytest.fixture
def refund_policy():
    return PolicySet.from_dir("policies").get(ACTION)


# --- the three refund verdicts -----------------------------------------------


def test_refund_within_ceiling_allows(refund_policy):
    d = evaluate(ACTION, refund_manifest(8000, 4000), refund_policy, NOW)
    assert d.effect is Effect.ALLOW


def test_refund_over_ceiling_restricts(refund_policy):
    d = evaluate(ACTION, refund_manifest(8000, 6500), refund_policy, NOW)
    assert d.effect is Effect.RESTRICT


def test_refund_exceeding_order_total_blocks(refund_policy):
    d = evaluate(ACTION, refund_manifest(8000, 9000), refund_policy, NOW)
    assert d.effect is Effect.BLOCK
    assert any("comparison failed" in r.reason for r in d.results)


def test_refund_exactly_at_ceiling_allows(refund_policy):
    # <= is inclusive: exactly 5000 is still a full auto-approve.
    d = evaluate(ACTION, refund_manifest(8000, 5000), refund_policy, NOW)
    assert d.effect is Effect.ALLOW


# --- RESTRICT executes a degraded payload via the gate decorator -------------


def test_restrict_executes_degraded_payload():
    gate = Gate(PolicySet.from_dir("policies"))

    @gate.enforce(action=ACTION)
    def issue_refund(payload, effect):
        amount = payload["refund_amount"]
        if effect is Effect.RESTRICT:
            amount = min(amount, 5000)
        return {"refunded": amount, "mode": effect.value}

    out = issue_refund(
        {"order_id": "order/1001", "refund_amount": 6500},
        manifest=refund_manifest(8000, 6500),
        request_id="r1",
        now=NOW,
    )
    assert out == {"refunded": 5000, "mode": "restrict"}


# --- comparison operand handling ---------------------------------------------


def _one(key, value, observed=True, item_id="x"):
    return EvidenceManifest(
        items=[
            EvidenceItem(
                id=item_id,
                claim="c",
                key=key,
                value=value,
                source=EvidenceSource.TOOL_RESULT,
                source_id="s",
                observed_at=NOW,
                observed=observed,
            )
        ],
        compiled_at=NOW,
    )


def _rule_with(cmp: Comparison) -> Rule:
    return Rule(id="c", compare=cmp, effect_on_fail=Effect.BLOCK)


def _check(cmp, manifest):
    # Evaluate a single-rule policy inline.
    from evidence_gate.policy import Policy

    policy = Policy(version="t", action="t", rules=[_rule_with(cmp)])
    return evaluate("t", manifest, policy, NOW).effect


def test_missing_left_operand_fails():
    cmp = Comparison(left_key="refund.amount", op="<=", right_value=100)
    assert _check(cmp, EvidenceManifest(items=[], compiled_at=NOW)) is Effect.BLOCK


def test_missing_right_key_operand_fails():
    cmp = Comparison(left_key="refund.amount", op="<=", right_key="order.total")
    assert _check(cmp, _one("refund.amount", 50)) is Effect.BLOCK


def test_relational_op_needs_numeric_operands():
    cmp = Comparison(left_key="k", op="<=", right_value=100)
    # A string left value can't be relationally compared -> fail.
    assert _check(cmp, _one("k", "not-a-number")) is Effect.BLOCK


def test_equality_op_allows_non_numeric():
    cmp = Comparison(left_key="k", op="==", right_value="US")
    assert _check(cmp, _one("k", "US")) is Effect.ALLOW
    cmp_ne = Comparison(left_key="k", op="!=", right_value="US")
    assert _check(cmp_ne, _one("k", "US")) is Effect.BLOCK


def test_comparison_requires_exactly_one_rhs():
    with pytest.raises(ValueError):
        Comparison(left_key="a", op="<=", right_key="b", right_value=1)
    with pytest.raises(ValueError):
        Comparison(left_key="a", op="<=")


def test_bool_is_not_treated_as_number():
    # True must not sneak through a relational compare as 1.
    cmp = Comparison(left_key="k", op="<=", right_value=5)
    assert _check(cmp, _one("k", True)) is Effect.BLOCK
