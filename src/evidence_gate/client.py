"""Remote gate client + name-pattern instrumentation (COMPARISON.md §6 #2).

`RemoteGate` is a thin, **fail-closed** client of the gate service (service.py).
It keeps our own surface — `.check` / `.enforce` / `.auto_instrument` mirror the
in-process `Gate` — and adds the ergonomics that make remote enforcement painless:
wrap existing tools by name pattern, refuse to execute if the gate is unreachable,
and carry a signed clearance token back.

Control-flow contract, identical to the in-process decorator (gate.py):

  * ALLOW / RESTRICT  → the wrapped tool runs; a `RemoteResult` carries the token.
  * REVIEW            → a pending `RemoteResult` is returned; the tool does *not*
                        run and the agent loop keeps control.
  * BLOCK             → `ClearanceDenied(.reason, .request_id)` is raised.
  * gate unreachable  → `GateUnreachable` is raised and the tool does *not* run
                        (fail closed — an unavailable gate never means "allow").

Needs the `client` extra:  uv sync --extra client
"""

from __future__ import annotations

import fnmatch
import functools
from datetime import datetime
from types import ModuleType
from typing import Any, Callable

import httpx

from evidence_gate.errors import ClearanceDenied, GateUnreachable
from evidence_gate.schemas import Decision, Effect, EvidenceManifest, ProposedAction
from evidence_gate.trace import ToolCall
from evidence_gate.signing import Verifier

__all__ = [
    "ClearanceDenied",
    "GateUnreachable",
    "RemoteGate",
    "RemoteResult",
]


class RemoteResult:
    """What `check()` returns: the decision, plus token/ticket when relevant."""

    def __init__(
        self,
        effect: Effect,
        decision: Decision,
        token: str | None = None,
        review_ticket: str | None = None,
        claims: dict | None = None,
    ) -> None:
        self.effect = effect
        self.decision = decision
        self.token = token
        self.review_ticket = review_ticket
        self.claims = claims  # verified token claims, when a Verifier was supplied

    @property
    def allowed(self) -> bool:
        return self.effect in (Effect.ALLOW, Effect.RESTRICT)


class RemoteGate:
    """Fail-closed HTTP client for the gate service."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        api_key: str | None = None,
        verifier: Verifier | None = None,
        timeout: float = 5.0,
        transport: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verifier = verifier
        # `transport` lets tests inject a FastAPI TestClient (same .post/.get
        # shape). In production it's a real httpx.Client.
        if transport is not None:
            self._http = transport
        else:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            self._http = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)

    def check(
        self,
        action: ProposedAction,
        *,
        manifest: EvidenceManifest | None = None,
        tool_calls: list[ToolCall] | None = None,
        now: datetime | None = None,
    ) -> RemoteResult:
        body: dict[str, Any] = {"action": action.model_dump(mode="json")}
        if manifest is not None:
            body["manifest"] = manifest.model_dump(mode="json")
        if tool_calls is not None:
            body["tool_calls"] = [tc.model_dump(mode="json") for tc in tool_calls]

        try:
            resp = self._http.post("/v1/check", json=body)
        except Exception as exc:  # connection error, timeout, DNS, ...
            raise GateUnreachable(f"gate at {self.base_url} unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise GateUnreachable(f"gate returned {resp.status_code}")
        if resp.status_code >= 400:
            # A malformed request is a client bug, not a fail-closed condition,
            # but we still refuse to execute — surface it loudly.
            raise GateUnreachable(f"gate rejected request ({resp.status_code}): {resp.text}")

        data = resp.json()
        decision = Decision.model_validate(data["decision"])
        effect = Effect(data["effect"])

        if effect == Effect.BLOCK:
            reason = "; ".join(data.get("reasons") or []) or "blocked"
            raise ClearanceDenied(decision, reason, action.request_id)

        claims = None
        token = data.get("token")
        if token and self.verifier is not None:
            # Verify on receipt; a bad token from a "gate" is a trust failure ->
            # fail closed rather than trust an unverifiable clearance.
            from evidence_gate.signing import TokenExpired, TokenInvalid

            try:
                claims = self.verifier.verify(token, now=now or _client_now())
            except (TokenInvalid, TokenExpired) as exc:
                raise GateUnreachable(f"clearance token failed verification: {exc}") from exc

        return RemoteResult(
            effect=effect,
            decision=decision,
            token=token,
            review_ticket=data.get("review_ticket"),
            claims=claims,
        )

    def enforce(self, action: str) -> Callable:
        """Decorator: gate a tool over the wire before it runs.

        The wrapped fn is called with `payload`, plus `manifest=` or
        `tool_calls=` for the evidence. Same contract as gate.enforce: raises on
        BLOCK, returns a pending `RemoteResult` on REVIEW, runs on ALLOW/RESTRICT
        (passing `effect=` so the tool can degrade a RESTRICTed payload itself).
        """

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(
                payload: dict,
                *,
                manifest: EvidenceManifest | None = None,
                tool_calls: list[ToolCall] | None = None,
                actor: str = "agent",
                request_id: str,
                now: datetime | None = None,
                **kwargs,
            ):
                proposed = ProposedAction(
                    action=action, payload=payload, actor=actor, request_id=request_id
                )
                result = self.check(proposed, manifest=manifest, tool_calls=tool_calls, now=now)
                if result.effect == Effect.REVIEW:
                    return result  # pending; do not execute
                return fn(payload, effect=result.effect, **kwargs)

            return wrapper

        return decorator

    def auto_instrument(self, target: ModuleType | dict, mapping: dict[str, str]) -> None:
        """Wrap tools in place by name pattern, mapping each to a gate action.

        `mapping` is `{name_glob: action}`, e.g. `{"stripe_*": "billing.issue_refund"}`.
        Every callable in `target` (a module or a name→fn dict) whose name matches
        a pattern is replaced with a gated wrapper. Ungated tools are untouched.

        The wrapper reads its evidence from a `manifest=` / `tool_calls=` kwarg and
        derives `request_id` from a `request_id=` kwarg (falling back to the tool
        name), so existing call sites gate transparently once the kwargs are
        supplied — zero-touch instrumentation with no per-call-site rewrite.
        """
        items = target.items() if isinstance(target, dict) else vars(target).items()
        for name, obj in list(items):
            if not callable(obj):
                continue
            action = _match_action(name, mapping)
            if action is None:
                continue
            wrapped = self._wrap_bare(obj, action)
            if isinstance(target, dict):
                target[name] = wrapped
            else:
                setattr(target, name, wrapped)

    def _wrap_bare(self, fn: Callable, action: str) -> Callable:
        """Wrap an arbitrary tool fn, pulling gate kwargs out of its call."""

        @functools.wraps(fn)
        def wrapper(
            *args,
            manifest: EvidenceManifest | None = None,
            tool_calls: list[ToolCall] | None = None,
            actor: str = "agent",
            request_id: str | None = None,
            now: datetime | None = None,
            **kwargs,
        ):
            proposed = ProposedAction(
                action=action,
                payload={"args": list(args), "kwargs": kwargs},
                actor=actor,
                request_id=request_id or f"{getattr(fn, '__name__', 'tool')}-call",
            )
            result = self.check(proposed, manifest=manifest, tool_calls=tool_calls, now=now)
            if result.effect == Effect.REVIEW:
                return result
            return fn(*args, **kwargs)

        return wrapper


def _match_action(name: str, mapping: dict[str, str]) -> str | None:
    for pattern, action in mapping.items():
        if fnmatch.fnmatch(name, pattern):
            return action
    return None


def _client_now() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)
