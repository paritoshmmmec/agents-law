Minimal viable experience:

1. **One killer demo path**
   User runs `uv run examples/demo_agent.py` and sees one sensitive action attempt each for `ALLOW`, `REVIEW`, `BLOCK`, and maybe `RESTRICT`.

2. **One real adoption path**
   Expose `RemoteGate.auto_instrument(tools, {"stripe_*": "billing.issue_refund"})` as the main story. Existing tool calls stay unchanged; unsafe calls fail closed.

3. **One onboarding path before enforcement**
   Let users replay an existing trace with `examples/trace_replay.py` and get a report: “these calls would allow, these would review, these would block.”

4. **One policy users can understand**
   Keep `policies/refund.yaml` or `marketing.yaml` as the canonical readable example. The user should be able to edit one threshold and rerun.

5. **One audit proof**
   Show the hash-chained audit log after the run, with request id, action, verdict, reasons, and chain hash.

MVE tagline: **“Wrap one dangerous tool, replay yesterday’s traces, then enforce the same policy live with deterministic allow/review/block decisions and an audit trail.”**

I would avoid expanding framework adapters or policy-compilation UX yet. The product center is: trace replay → confidence → fail-closed live wrapper.