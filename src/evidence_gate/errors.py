"""Enforcement exceptions, dependency-free.

These live apart from `client.py` (which needs the optional `httpx`) so the
framework adapters and their shared `GateSession` can raise/catch them without
pulling in the client extra. `client.py` re-exports them, so
`from evidence_gate.client import ClearanceDenied` keeps working.
"""

from __future__ import annotations

from evidence_gate.schemas import Decision


class ClearanceDenied(Exception):
    """Raised when the gate BLOCKs an action. Mirrors gate.ActionBlocked."""

    def __init__(self, decision: Decision, reason: str, request_id: str) -> None:
        self.decision = decision
        self.reason = reason
        self.request_id = request_id
        super().__init__(f"clearance denied ({request_id}): {reason}")


class GateUnreachable(Exception):
    """The gate service could not be reached / returned an error. Fail closed."""
