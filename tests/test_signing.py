"""Signing tests: token round-trip, expiry, tamper, and chain-hash compat."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from evidence_gate.signing import (
    ClearanceRequired,
    Signer,
    TokenExpired,
    TokenInvalid,
    Verifier,
    require_clearance,
)

KEY = b"unit-test-key"


def test_chain_hash_unkeyed_matches_plain_sha256(now):
    # Regression guard: an unsigned Signer must hash byte-identically to the
    # original audit `_hash` = sha256(prev + payload).
    signer = Signer()  # key=None
    prev, payload = "0" * 64, '{"a":1}'
    expected = hashlib.sha256(prev.encode() + payload.encode()).hexdigest()
    assert signer.chain_hash(prev, payload) == expected


def test_chain_hash_keyed_differs_from_unkeyed(now):
    prev, payload = "0" * 64, '{"a":1}'
    assert Signer(KEY).chain_hash(prev, payload) != Signer().chain_hash(prev, payload)


def test_token_round_trip(now):
    signer = Signer(KEY)
    token = signer.issue({"request_id": "r1", "effect": "allow"}, ttl_seconds=300, now=now)
    claims = Verifier(KEY).verify(token, now=now)
    assert claims["request_id"] == "r1"
    assert claims["effect"] == "allow"
    assert "exp" in claims


def test_token_expires(now):
    signer = Signer(KEY)
    token = signer.issue({"request_id": "r1"}, ttl_seconds=300, now=now)
    later = now + timedelta(seconds=301)
    with pytest.raises(TokenExpired):
        Verifier(KEY).verify(token, now=later)


def test_token_still_valid_within_ttl(now):
    signer = Signer(KEY)
    token = signer.issue({"request_id": "r1"}, ttl_seconds=300, now=now)
    Verifier(KEY).verify(token, now=now + timedelta(seconds=299))  # no raise


def test_wrong_key_fails_verification(now):
    token = Signer(KEY).issue({"request_id": "r1"}, ttl_seconds=300, now=now)
    with pytest.raises(TokenInvalid):
        Verifier(b"other-key").verify(token, now=now)


def test_tampered_payload_fails_verification(now):
    token = Signer(KEY).issue({"request_id": "r1"}, ttl_seconds=300, now=now)
    payload, sig = token.split(".")
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + sig
    with pytest.raises(TokenInvalid):
        Verifier(KEY).verify(tampered, now=now)


def test_malformed_token_fails(now):
    with pytest.raises(TokenInvalid):
        Verifier(KEY).verify("not-a-token", now=now)


def test_unkeyed_signer_cannot_issue(now):
    with pytest.raises(ValueError):
        Signer().issue({"request_id": "r1"}, ttl_seconds=300, now=now)


# --- require_clearance: the downstream guard -------------------------------
ACTION = "billing.issue_refund"


def _refund(clock):
    """A downstream effect guarded by clearance, with an injected clock."""

    @require_clearance(Verifier(KEY), action=ACTION, now=lambda: clock)
    def execute_refund(amount):  # token is consumed by the guard, not forwarded here
        return amount

    return execute_refund


def _token(now, *, action=ACTION, ttl=300):
    return Signer(KEY).issue(
        {"request_id": "r1", "action": action, "effect": "allow"}, ttl_seconds=ttl, now=now
    )


def test_require_clearance_runs_with_valid_token(now):
    assert _refund(now)(100, clearance_token=_token(now)) == 100


def test_require_clearance_refuses_missing_token(now):
    with pytest.raises(ClearanceRequired, match="missing clearance token"):
        _refund(now)(100)


def test_require_clearance_refuses_forged_token(now):
    forged = Signer(b"wrong-key").issue({"action": ACTION}, ttl_seconds=300, now=now)
    with pytest.raises(ClearanceRequired, match="invalid clearance token"):
        _refund(now)(100, clearance_token=forged)


def test_require_clearance_refuses_expired_token(now):
    later = now + timedelta(seconds=600)  # clock is past the 300s ttl
    with pytest.raises(ClearanceRequired, match="expired"):
        _refund(later)(100, clearance_token=_token(now, ttl=300))


def test_require_clearance_binds_action(now):
    # A token minted for a different action must not clear this one.
    other = _token(now, action="billing.wire_transfer")
    with pytest.raises(ClearanceRequired, match="not 'billing.issue_refund'"):
        _refund(now)(100, clearance_token=other)


def test_require_clearance_does_not_forward_token(now):
    # The guard pops the token; the wrapped fn's signature stays clean.
    @require_clearance(Verifier(KEY), now=lambda: now)
    def strict(amount):  # no **kwargs — would TypeError if token leaked through
        return amount

    assert strict(5, clearance_token=_token(now)) == 5
