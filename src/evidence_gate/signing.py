"""Signing primitives — the audit-chain hash and short-lived clearance tokens.

Two jobs, one key:

  * `Signer.chain_hash()` is the drop-in the audit log documents at
    `audit.py` — with `key=None` it is exactly today's `sha256(prev + payload)`,
    so an unsigned log hashes byte-identically to before. Given an HMAC key it
    binds the chain to that key, upgrading the tamper-evident trail to a keyed
    one (DESIGN §13.5).
  * `Signer.issue()` / `Verifier.verify()` mint and check a compact clearance
    token the gate service returns on ALLOW/RESTRICT. The token proves *this
    request* cleared *this policy version* at *this time*, and expires.

The token format is deliberately our own — a base64url(payload) . base64url(sig)
pair, HMAC-SHA256 over the payload — so the library stays dependency-free and
consistent with keeping our own surface. Swapping HMAC for an asymmetric
signature later touches only this module.

`now` is always injected, never read from the wall clock, so expiry is
reproducible in tests and in audit replay — the same determinism contract the
engine holds (engine.py §5.5).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone


class TokenInvalid(Exception):
    """Token signature did not verify, or the token is malformed."""


class TokenExpired(Exception):
    """Token signature is valid but its `exp` is in the past relative to `now`."""


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class Signer:
    """Mints chain hashes and clearance tokens. `key=None` = unsigned (hash only)."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key

    @property
    def keyed(self) -> bool:
        return self._key is not None

    def chain_hash(self, prev_hash: str, payload: str) -> str:
        """Hash one audit record onto the chain.

        Unkeyed, this is `sha256(prev_hash + payload)` — byte-identical to the
        log's original `_hash`, so existing chains and tests are unaffected.
        Keyed, it is `HMAC-SHA256(key, prev_hash + payload)`.
        """
        message = prev_hash.encode() + payload.encode()
        if self._key is None:
            return hashlib.sha256(message).hexdigest()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def issue(self, claims: dict, *, ttl_seconds: int, now: datetime) -> str:
        """Mint a token carrying `claims` plus an `exp` = now + ttl.

        Requires a key — an unsigned token would be forgeable and is refused.
        """
        if self._key is None:
            raise ValueError("cannot issue a token without a signing key")
        exp = int(now.timestamp()) + ttl_seconds
        payload = {**claims, "exp": exp}
        payload_b = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self._key, payload_b, hashlib.sha256).digest()
        return f"{_b64u_encode(payload_b)}.{_b64u_encode(sig)}"


class Verifier:
    """Verifies clearance tokens minted by a `Signer` with the same key."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def verify(self, token: str, *, now: datetime) -> dict:
        """Return the token's claims, or raise `TokenInvalid` / `TokenExpired`."""
        try:
            payload_part, sig_part = token.split(".")
            payload_b = _b64u_decode(payload_part)
            sig = _b64u_decode(sig_part)
        except Exception as exc:  # malformed encoding/shape
            raise TokenInvalid("malformed token") from exc

        expected = hmac.new(self._key, payload_b, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise TokenInvalid("signature mismatch")

        claims = json.loads(payload_b)
        exp = claims.get("exp")
        if exp is not None and now.timestamp() > exp:
            raise TokenExpired(f"token expired at {datetime.fromtimestamp(exp, timezone.utc).isoformat()}")
        return claims
