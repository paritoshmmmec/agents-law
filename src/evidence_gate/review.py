"""Human-in-the-loop routing (DESIGN.md §8).

When a decision is REVIEW, the gate hands the *full assembled context* (action +
manifest + decision) to a `ReviewQueue` and returns without raising — so the
agent's loop keeps running. A human or a separate eval agent resolves the ticket
later; the resolution is itself auditable.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol

from pydantic import BaseModel, Field

from evidence_gate.schemas import Decision, Effect, EvidenceManifest, ProposedAction


class ReviewTicket(BaseModel):
    """A pending action parked for human/eval review. Context is never dropped."""

    ticket_id: str
    action: ProposedAction
    manifest: EvidenceManifest
    decision: Decision
    resolved: bool = False
    approver: str | None = None
    resolved_effect: Effect | None = None


class ReviewQueue(Protocol):
    """The routing seam. Swap the in-memory impl for a real queue in production."""

    def enqueue(
        self,
        decision: Decision,
        action: ProposedAction,
        manifest: EvidenceManifest,
    ) -> str: ...

    def resolve(self, ticket_id: str, approver: str, effect: Effect) -> ReviewTicket: ...


class InMemoryReviewQueue:
    """Minimal queue for the demo and tests."""

    def __init__(self) -> None:
        self._tickets: dict[str, ReviewTicket] = {}
        self._counter = 0

    def enqueue(
        self,
        decision: Decision,
        action: ProposedAction,
        manifest: EvidenceManifest,
    ) -> str:
        self._counter += 1
        ticket_id = f"rev-{self._counter:04d}"
        self._tickets[ticket_id] = ReviewTicket(
            ticket_id=ticket_id,
            action=action,
            manifest=manifest,
            decision=decision,
        )
        return ticket_id

    def resolve(self, ticket_id: str, approver: str, effect: Effect) -> ReviewTicket:
        ticket = self._tickets[ticket_id]
        ticket.resolved = True
        ticket.approver = approver
        ticket.resolved_effect = effect
        return ticket

    def get(self, ticket_id: str) -> ReviewTicket:
        return self._tickets[ticket_id]

    def pending(self) -> list[ReviewTicket]:
        return [t for t in self._tickets.values() if not t.resolved]


class SQLiteReviewQueue:
    """A durable `ReviewQueue` backed by SQLite (DESIGN §8, persistence tail).

    Same seam as `InMemoryReviewQueue`, but tickets survive a restart and are
    visible across processes — the review step is no longer lost when the host
    recycles. Each ticket is stored as one row; the full `ReviewTicket` JSON is
    the source of truth, with `ticket_id` / `resolved` mirrored into columns so
    `pending()` is an indexed query rather than a full scan-and-parse.

    The ticket id is derived from the row's autoincrement `seq`, so ids stay
    monotonic and collision-free even when several processes enqueue at once —
    the database, not a Python counter, hands out the sequence. A `busy_timeout`
    lets concurrent writers wait for the lock instead of failing.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_tickets (
                    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT UNIQUE,
                    resolved  INTEGER NOT NULL DEFAULT 0,
                    data      TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_review_tickets_pending "
                "ON review_tickets (resolved)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # One connection per operation keeps the queue safe to share across
        # threads and processes; WAL + a busy timeout let concurrent writers
        # coexist. The `with conn` block commits on success / rolls back on error.
        conn = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000)
        try:
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def enqueue(
        self,
        decision: Decision,
        action: ProposedAction,
        manifest: EvidenceManifest,
    ) -> str:
        with self._connect() as conn:
            # Insert first to let SQLite assign the seq atomically, then stamp
            # the derived id back onto the same row in one transaction.
            cur = conn.execute(
                "INSERT INTO review_tickets (ticket_id, resolved, data) VALUES (NULL, 0, '')"
            )
            seq = cur.lastrowid
            ticket_id = f"rev-{seq:04d}"
            ticket = ReviewTicket(
                ticket_id=ticket_id,
                action=action,
                manifest=manifest,
                decision=decision,
            )
            conn.execute(
                "UPDATE review_tickets SET ticket_id = ?, data = ? WHERE seq = ?",
                (ticket_id, ticket.model_dump_json(), seq),
            )
        return ticket_id

    def resolve(self, ticket_id: str, approver: str, effect: Effect) -> ReviewTicket:
        with self._connect() as conn:
            ticket = self._load(conn, ticket_id)
            ticket.resolved = True
            ticket.approver = approver
            ticket.resolved_effect = effect
            conn.execute(
                "UPDATE review_tickets SET resolved = 1, data = ? WHERE ticket_id = ?",
                (ticket.model_dump_json(), ticket_id),
            )
        return ticket

    def get(self, ticket_id: str) -> ReviewTicket:
        with self._connect() as conn:
            return self._load(conn, ticket_id)

    def pending(self) -> list[ReviewTicket]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM review_tickets WHERE resolved = 0 ORDER BY seq"
            ).fetchall()
        return [ReviewTicket.model_validate_json(row[0]) for row in rows]

    def _load(self, conn: sqlite3.Connection, ticket_id: str) -> ReviewTicket:
        row = conn.execute(
            "SELECT data FROM review_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise KeyError(ticket_id)
        return ReviewTicket.model_validate_json(row[0])
