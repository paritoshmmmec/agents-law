# Remote gate (service + fail-closed client)

The same pure `check()` lifts behind FastAPI, so the engine and policies can live in
one place and agents call it over the wire. The client is **fail-closed**: an
unreachable gate never means "allow."

```python
# server — the gate service
from evidence_gate import Gate, PolicySet, Signer, create_app

app = create_app(Gate(PolicySet.from_dir("policies")), signer=Signer(b"secret-key"))
# uvicorn: `create_app` returns a FastAPI app with /v1/check, /v1/review, /v1/audit
```

```python
# client — wrap existing tools by name pattern, no per-call-site rewrite
from evidence_gate import RemoteGate

gate = RemoteGate("https://gate.internal")
gate.auto_instrument(tools, {"stripe_*": "billing.issue_refund"})

tools.stripe_refund(45, "late package")   # gated transparently; runs only on ALLOW/RESTRICT
#   BLOCK            -> raises ClearanceDenied(.reason, .request_id)
#   gate unreachable -> raises GateUnreachable (tool never runs)
```

On `ALLOW`/`RESTRICT` the service returns a short-lived **HMAC-signed clearance
token** (`Signer`/`Verifier`); the client verifies it on receipt. The same key
signs the audit chain — with `key=None` the chain hash is byte-identical to the
plain hash, so signing is a backwards-compatible drop-in.

A downstream service that actually executes the effect can make that token
**load-bearing** — refusing any call that doesn't carry a fresh, action-bound one:

```python
from evidence_gate import Verifier, require_clearance

@require_clearance(Verifier(b"secret-key"), action="billing.issue_refund")
def execute_refund(amount):          # the token is consumed by the guard, not forwarded
    ledger.debit(amount)

execute_refund(45, clearance_token=token)   # runs only if the token verifies for this action
#   missing / forged / expired token -> raises ClearanceRequired (effect never runs)
#   token minted for another action  -> raises ClearanceRequired
```
