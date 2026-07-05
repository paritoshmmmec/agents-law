"""Gate tests: structural rejection, routing, audit-before-return, decorator."""

from __future__ import annotations

import pytest

from evidence_gate import (
    ActionBlocked,
    Effect,
    Gate,
    ProposedAction,
)

from tests.conftest import manifest, opt_in

ACTION = "marketing.send_sequence"


@pytest.fixture
def gate(policies):
    return Gate(policies)


def action(request_id: str = "r1") -> ProposedAction:
    return ProposedAction(
        action=ACTION, payload={"contact": 1}, actor="agent", request_id=request_id
    )


def test_missing_manifest_blocks_and_is_audited(gate, now):
    result = gate.check(action(), None, now=now)
    assert result.effect is Effect.BLOCK
    # Requirement #4/#5: never a silent drop — the block is recorded.
    assert len(gate.audit.records) == 1
    assert gate.audit.records[0].decision.effect is Effect.BLOCK


def test_allow_is_audited(gate, now):
    gate.check(action(), manifest(opt_in(60)), now=now)
    assert len(gate.audit.records) == 1
    assert gate.audit.records[0].decision.effect is Effect.ALLOW


def test_review_enqueues_ticket(gate, now):
    result = gate.check(action(), manifest(opt_in(425)), now=now)
    assert result.effect is Effect.REVIEW
    assert result.review_ticket is not None
    assert len(gate.review.pending()) == 1


def test_review_resolution_is_audited(gate, now):
    # DESIGN §8: resolving a review is itself audited, with approver set.
    result = gate.check(action("req-stale"), manifest(opt_in(425)), now=now)
    assert result.effect is Effect.REVIEW
    assert len(gate.audit.records) == 1

    ticket = gate.resolve_review(
        result.review_ticket, approver="ops@corp", effect=Effect.ALLOW, now=now
    )
    assert ticket.resolved and ticket.approver == "ops@corp"
    # A second, approver-stamped record was appended and the chain still holds.
    assert len(gate.audit.records) == 2
    last = gate.audit.records[-1]
    assert last.approver == "ops@corp"
    assert last.decision.effect is Effect.ALLOW
    assert last.request_id == "req-stale"
    assert gate.audit.verify()
    assert not gate.review.pending()


def test_decision_carries_request_id(gate, now):
    result = gate.check(action("req-xyz"), manifest(opt_in(60)), now=now)
    assert result.decision.request_id == "req-xyz"


def test_every_check_is_recorded(gate, now):
    gate.check(action("a"), manifest(opt_in(60)), now=now)
    gate.check(action("b"), None, now=now)
    gate.check(action("c"), manifest(opt_in(425)), now=now)
    assert len(gate.audit.records) == 3
    assert gate.audit.verify()


# --- decorator ---------------------------------------------------------------


def test_enforce_executes_on_allow(gate, now):
    @gate.enforce(action=ACTION)
    def send(payload, effect):
        return "sent"

    out = send({"contact": 1}, manifest=manifest(opt_in(60)), request_id="r1", now=now)
    assert out == "sent"


def test_enforce_raises_on_block(gate, now):
    @gate.enforce(action=ACTION)
    def send(payload, effect):
        return "sent"

    with pytest.raises(ActionBlocked):
        send({"contact": 1}, manifest=manifest(), request_id="r1", now=now)


def test_enforce_returns_pending_on_review(gate, now):
    @gate.enforce(action=ACTION)
    def send(payload, effect):
        return "sent"

    result = send({"contact": 1}, manifest=manifest(opt_in(425)), request_id="r1", now=now)
    assert result.effect is Effect.REVIEW  # did NOT execute
