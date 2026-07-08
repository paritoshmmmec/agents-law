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
import functools
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


class TokenInvalid(Exception):
    """Token signature did not verify, or the token is malformed."""


class TokenExpired(Exception):
    """Token signature is valid but its `exp` is in the past relative to `now`."""


class ClearanceRequired(Exception):
    """A downstream call was refused because it carried no valid clearance token.

    The umbrella a downstream service catches to fail closed: raised for a missing,
    malformed, expired, or wrong-action token alike, with `.reason` explaining which.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def require_clearance(
    verifier: Verifier,
    *,
    action: str | None = None,
    token_arg: str = "clearance_token",
    now: Callable[[], datetime] = _now_utc,
) -> Callable:
    """Guard a downstream tool so it refuses any call lacking a valid clearance token.

    This is what makes the gate *load-bearing* downstream: an ALLOW/RESTRICT mints a
    short-lived signed token (`Signer.issue`), and a service that actually executes
    the consequential effect wraps its entrypoint in this guard. A call with no
    token, a forged/expired token, or a token minted for a *different* action is
    refused with `ClearanceRequired` — the effect never runs. Fail-closed by
    construction: the default is deny, and only a fresh valid token opens it.

        @require_clearance(verifier, action="billing.issue_refund")
        def execute_refund(amount, *, clearance_token):
            ...  # runs only when the token verifies for this action

    The token is read from the `token_arg` keyword (default ``clearance_token``) and
    is *not* forwarded to the wrapped function, so existing signatures stay clean.
    Binding `action` rejects a token good for some other action — a refund token
    can't clear a wire transfer. `now` is injectable for reproducible tests.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = kwargs.pop(token_arg, None)
            if not token:
                raise ClearanceRequired(f"missing clearance token (expected keyword {token_arg!r})")
            try:
                claims = verifier.verify(token, now=now())
            except TokenExpired as exc:
                raise ClearanceRequired(f"clearance token expired: {exc}") from exc
            except TokenInvalid as exc:
                raise ClearanceRequired(f"invalid clearance token: {exc}") from exc
            if action is not None and claims.get("action") != action:
                raise ClearanceRequired(
                    f"clearance token is for action {claims.get('action')!r}, not {action!r}"
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator
