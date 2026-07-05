"""Audit tests: hash-chain integrity and tamper detection."""

from __future__ import annotations

from evidence_gate import Decision, Effect, ProposedAction
from evidence_gate.audit import GENESIS_HASH, AuditLog

from tests.conftest import manifest, opt_in


def _append(log: AuditLog, request_id: str, now):
    action = ProposedAction(
        action="marketing.send_sequence",
        payload={"x": 1},
        actor="agent",
        request_id=request_id,
    )
    decision = Decision(
        effect=Effect.ALLOW, results=[], request_id=request_id, decided_at=now
    )
    return log.append(action, manifest(opt_in(60)), decision, now)


def test_first_record_links_to_genesis(now):
    log = AuditLog()
    rec = _append(log, "r1", now)
    assert rec.prev_hash == GENESIS_HASH
    assert rec.seq == 0


def test_chain_links_records(now):
    log = AuditLog()
    r0 = _append(log, "r1", now)
    r1 = _append(log, "r2", now)
    assert r1.prev_hash == r0.hash
    assert log.verify()


def test_tamper_breaks_chain(now):
    log = AuditLog()
    _append(log, "r1", now)
    _append(log, "r2", now)
    assert log.verify()

    # Mutate a past record's content -> its stored hash no longer matches.
    log.records[0].action.payload = {"x": 999}
    assert not log.verify()


def test_reordering_breaks_chain(now):
    log = AuditLog()
    _append(log, "r1", now)
    _append(log, "r2", now)
    log.records.reverse()
    assert not log.verify()


def test_file_sink_writes_jsonl(tmp_path, now):
    path = tmp_path / "audit.log"
    log = AuditLog(path=path)
    _append(log, "r1", now)
    _append(log, "r2", now)
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
