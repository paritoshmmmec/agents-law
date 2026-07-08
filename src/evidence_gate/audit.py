"""Append-only, hash-chained audit log (DESIGN.md §7).

Every gate decision produces one record capturing the proposed payload, the
evidence, the rules that fired, and the routing decision. Records are linked in
a hash chain: editing any past record breaks the chain from that point on, which
gives us a tamper-evident ("signed") trail without key management yet. Swapping
`_hash` for an HMAC/asymmetric signature is a drop-in upgrade.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr

from evidence_gate.schemas import Decision, EvidenceManifest, ProposedAction
from evidence_gate.signing import Signer

GENESIS_HASH = "0" * 64


class AuditRecord(BaseModel):
    """One immutable entry in the audit chain."""

    seq: int
    request_id: str
    action: ProposedAction
    manifest: EvidenceManifest
    decision: Decision
    policy_version: str | None
    approver: str | None = None  # set when a human resolves a REVIEW
    recorded_at: datetime
    prev_hash: str
    hash: str = ""  # filled in by the log at append time

    def payload_for_hash(self) -> str:
        """Canonical serialization of everything except `hash` itself."""
        data = self.model_dump(mode="json", exclude={"hash"})
        return json.dumps(data, sort_keys=True, separators=(",", ":"))


class AuditLog(BaseModel):
    """In-memory hash-chained log with optional append to a JSONL file.

    Kept simple on purpose: the invariant that matters is that each record's
    `hash` binds its content to the previous record's `hash`.

    A `Signer` supplies the chain hash. The default is an *unsigned* signer, so
    the chain is byte-identical to a plain `sha256(prev + payload)` — passing a
    keyed `Signer` upgrades the trail to a keyed (HMAC) one with no other change.
    """

    records: list[AuditRecord] = Field(default_factory=list)
    path: Path | None = None
    _signer: Signer = PrivateAttr(default_factory=Signer)

    def __init__(self, path: Path | None = None, *, signer: Signer | None = None, **data) -> None:
        super().__init__(path=path, **data)
        if signer is not None:
            self._signer = signer

    def _hash(self, prev_hash: str, record: AuditRecord) -> str:
        return self._signer.chain_hash(prev_hash, record.payload_for_hash())

    def append(
        self,
        action: ProposedAction,
        manifest: EvidenceManifest,
        decision: Decision,
        now: datetime,
        approver: str | None = None,
    ) -> AuditRecord:
        prev_hash = self.records[-1].hash if self.records else GENESIS_HASH
        record = AuditRecord(
            seq=len(self.records),
            request_id=decision.request_id,
            action=action,
            manifest=manifest,
            decision=decision,
            policy_version=decision.policy_version,
            approver=approver,
            recorded_at=now,
            prev_hash=prev_hash,
        )
        record.hash = self._hash(prev_hash, record)
        self.records.append(record)
        if self.path is not None:
            with self.path.open("a") as fh:
                fh.write(record.model_dump_json() + "\n")
        return record

    def verify(self) -> bool:
        """Recompute the chain; True iff no record has been tampered with."""
        prev_hash = GENESIS_HASH
        for record in self.records:
            if record.prev_hash != prev_hash:
                return False
            if record.hash != self._hash(prev_hash, record):
                return False
            prev_hash = record.hash
        return True
