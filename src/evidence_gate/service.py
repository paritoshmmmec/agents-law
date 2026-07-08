"""HTTP gate service — a FastAPI wrapper over Gate.check() (COMPARISON.md §6 #1).

A FastAPI wrapper over an existing `Gate`. It adds *no* evaluation logic: every
request converges on the same validated `EvidenceManifest` and the same pure
`gate.check()` the in-process library already uses. The only thing the service
does that the engine must never do is read the wall clock — `now` is stamped here
and injected downward, so the engine's determinism contract is intact.

Two manifest paths are supported (DESIGN §12.1, "both"):

  * the client POSTs a fully-formed `manifest` (agent-declared evidence), or
  * the client POSTs observed `tool_calls`, and the service reconstructs the
    manifest with a `ManifestBuilder`'s registered extractors.

On ALLOW/RESTRICT the response carries a short-lived signed clearance token
(§6 #3); on REVIEW/BLOCK it carries none.

`create_app` needs the `service` extra:  uv sync --extra service
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel, model_validator

from evidence_gate.gate import Gate
from evidence_gate.review import ReviewTicket
from evidence_gate.schemas import Decision, Effect, EvidenceManifest, ProposedAction
from evidence_gate.signing import Signer
from evidence_gate.trace import ManifestBuilder, ToolCall


class CheckRequest(BaseModel):
    """A gate check over the wire. Supply exactly one of `manifest` / `tool_calls`."""

    action: ProposedAction
    manifest: EvidenceManifest | None = None
    tool_calls: list[ToolCall] | None = None

    @model_validator(mode="after")
    def _one_evidence_path(self) -> CheckRequest:
        if self.manifest is not None and self.tool_calls is not None:
            raise ValueError("supply either manifest or tool_calls, not both")
        return self


class CheckResponse(BaseModel):
    """The gate's verdict, plus a clearance token when the action may execute."""

    effect: Effect
    executed: bool  # True when the caller may execute (ALLOW / RESTRICT)
    decision: Decision
    review_ticket: str | None = None
    token: str | None = None  # signed clearance, present only on ALLOW / RESTRICT
    reasons: list[str] = []  # non-ALLOW rule reasons, for a quick human read


class ResolveRequest(BaseModel):
    approver: str
    effect: Effect


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_app(
    gate: Gate,
    signer: Signer | None = None,
    builder: ManifestBuilder | None = None,
    token_ttl_seconds: int = 300,
) -> FastAPI:
    """Build the gate service app around an already-configured `Gate`.

    `signer` (keyed) enables clearance tokens; without one, ALLOW/RESTRICT still
    return but carry no token. `builder` enables the `tool_calls` path; without
    one, a `tool_calls` request reconstructs to an empty manifest (which the gate
    then blocks structurally — never a silent pass).
    """
    app = FastAPI(title="Evidence Gate", version="0.3.0")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/check", response_model=CheckResponse)
    def check(req: CheckRequest) -> CheckResponse:
        now = _utcnow()

        manifest = req.manifest
        if manifest is None and req.tool_calls is not None:
            # Server-reconstructed path. A tool with no registered extractor
            # contributes nothing (trace.py) — evidence stays opt-in.
            build = builder if builder is not None else ManifestBuilder()
            manifest = build.build(req.tool_calls, now)
        # If both are None the manifest stays None and gate.check() BLOCKs
        # structurally — the missing-manifest case is recorded, not dropped.

        result = gate.check(req.action, manifest, now=now)
        decision = result.decision

        token = None
        if signer is not None and signer.keyed and result.allowed:
            token = signer.issue(
                {
                    "request_id": req.action.request_id,
                    "action": req.action.action,
                    "effect": decision.effect.value,
                    "policy_version": decision.policy_version,
                    "decided_at": decision.decided_at.isoformat(),
                },
                ttl_seconds=token_ttl_seconds,
                now=now,
            )

        reasons = [r.reason for r in decision.results if r.effect != Effect.ALLOW]
        return CheckResponse(
            effect=decision.effect,
            executed=result.allowed,
            decision=decision,
            review_ticket=result.review_ticket,
            token=token,
            reasons=reasons,
        )

    @app.post("/v1/review/{ticket_id}/resolve", response_model=ReviewTicket)
    def resolve(ticket_id: str, req: ResolveRequest) -> ReviewTicket:
        return gate.resolve_review(ticket_id, req.approver, req.effect, now=_utcnow())

    @app.get("/v1/review/pending", response_model=list[ReviewTicket])
    def pending() -> list[ReviewTicket]:
        # The in-memory queue exposes pending(); a real queue would too.
        return getattr(gate.review, "pending", list)()

    @app.get("/v1/audit")
    def audit() -> list[dict]:
        return [r.model_dump(mode="json") for r in gate.audit.records]

    @app.get("/v1/audit/verify")
    def audit_verify() -> dict:
        return {"intact": gate.audit.verify()}

    return app
