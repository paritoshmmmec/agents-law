"""Review-queue tests: the durable SQLite backend mirrors the in-memory one and
survives a restart (a fresh instance over the same file)."""

from __future__ import annotations

import pytest

from evidence_gate import (
    Decision,
    Effect,
    Gate,
    ProposedAction,
    SQLiteReviewQueue,
)
from evidence_gate.review import InMemoryReviewQueue

from tests.conftest import manifest, opt_in

ACTION = "marketing.send_sequence"


def _decision(request_id: str, now) -> Decision:
    return Decision(
        effect=Effect.REVIEW,
        results=[],
        request_id=request_id,
        decided_at=now,
        policy_version="v1",
    )


def _action(request_id: str = "r1") -> ProposedAction:
    return ProposedAction(
        action=ACTION, payload={"contact": 1}, actor="agent", request_id=request_id
    )


@pytest.fixture(params=["memory", "sqlite"])
def queue(request, tmp_path):
    """Both queue impls, run through the identical contract."""
    if request.param == "memory":
        return InMemoryReviewQueue()
    return SQLiteReviewQueue(tmp_path / "review.db")


def test_enqueue_returns_id_and_lists_pending(queue, now):
    ticket_id = queue.enqueue(_decision("r1", now), _action("r1"), manifest(opt_in(60)))
    assert ticket_id == "rev-0001"
    pending = queue.pending()
    assert len(pending) == 1
    assert pending[0].ticket_id == ticket_id
    assert pending[0].decision.request_id == "r1"


def test_enqueue_ids_are_monotonic(queue, now):
    first = queue.enqueue(_decision("a", now), _action("a"), manifest(opt_in(60)))
    second = queue.enqueue(_decision("b", now), _action("b"), manifest(opt_in(60)))
    assert first == "rev-0001"
    assert second == "rev-0002"


def test_resolve_marks_done_and_records_approver(queue, now):
    ticket_id = queue.enqueue(_decision("r1", now), _action("r1"), manifest(opt_in(60)))
    ticket = queue.resolve(ticket_id, approver="ops@corp", effect=Effect.ALLOW)
    assert ticket.resolved
    assert ticket.approver == "ops@corp"
    assert ticket.resolved_effect is Effect.ALLOW
    assert queue.pending() == []
    # get() reflects the persisted resolution, not a stale copy.
    assert queue.get(ticket_id).resolved


def test_get_unknown_ticket_raises(queue):
    with pytest.raises(KeyError):
        queue.get("rev-9999")


# --- SQLite-specific: durability across a restart ---------------------------


def test_sqlite_survives_restart(tmp_path, now):
    path = tmp_path / "review.db"
    q1 = SQLiteReviewQueue(path)
    ticket_id = q1.enqueue(_decision("r1", now), _action("r1"), manifest(opt_in(60)))

    # A fresh instance over the same file — simulates a process restart.
    q2 = SQLiteReviewQueue(path)
    assert len(q2.pending()) == 1
    reloaded = q2.get(ticket_id)
    assert reloaded.ticket_id == ticket_id
    assert reloaded.action.request_id == "r1"
    # Full manifest round-trips, not just the id.
    assert reloaded.manifest.items[0].key == "marketing.opt_in"


def test_sqlite_ids_continue_after_restart(tmp_path, now):
    path = tmp_path / "review.db"
    q1 = SQLiteReviewQueue(path)
    q1.enqueue(_decision("a", now), _action("a"), manifest(opt_in(60)))

    q2 = SQLiteReviewQueue(path)
    # The DB, not a Python counter, owns the sequence — no id reuse on restart.
    assert q2.enqueue(_decision("b", now), _action("b"), manifest(opt_in(60))) == "rev-0002"


def test_sqlite_resolution_persists(tmp_path, now):
    path = tmp_path / "review.db"
    q1 = SQLiteReviewQueue(path)
    ticket_id = q1.enqueue(_decision("r1", now), _action("r1"), manifest(opt_in(60)))
    q1.resolve(ticket_id, approver="ops@corp", effect=Effect.BLOCK)

    q2 = SQLiteReviewQueue(path)
    assert q2.pending() == []
    ticket = q2.get(ticket_id)
    assert ticket.resolved and ticket.resolved_effect is Effect.BLOCK


# --- the durable queue behind a real Gate -----------------------------------


def test_gate_routes_review_to_sqlite_queue(policies, tmp_path, now):
    queue = SQLiteReviewQueue(tmp_path / "review.db")
    gate = Gate(policies, review=queue)
    # A stale (425-day) opt-in routes to REVIEW under the shipped policy.
    result = gate.check(_action("stale"), manifest(opt_in(425)), now=now)
    assert result.effect is Effect.REVIEW
    assert result.review_ticket is not None

    # The ticket is durable: a fresh queue over the same file still sees it,
    # and resolving through the gate is audited as before.
    assert len(SQLiteReviewQueue(tmp_path / "review.db").pending()) == 1
    ticket = gate.resolve_review(result.review_ticket, "ops@corp", Effect.ALLOW, now=now)
    assert ticket.resolved
    assert gate.audit.verify()
