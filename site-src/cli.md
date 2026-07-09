# CLI (`evidence-gate`)

The same replay and audit-verify flows, from a shell — the Phase-1 onboarding
surface, no Python required beyond an importable extractor module.

```bash
# Diagnose: what would the gate have decided on yesterday's trace?
evidence-gate replay trace.json \
  --policy policies --mapping langsmith \
  --action 'send_*=marketing.send_sequence' \
  --extractor 'get_optin=myapp.extractors:optin'
#   s42  ALLOW  executed=true
#   s99  BLOCK  executed=false — missing required evidence for 'marketing.opt_in'
#   Coverage:  gated: send_marketing | ! UNCLASSIFIED: wire_transfer (x1)

# Verify a hash-chained audit log written by AuditLog(path=...)
evidence-gate audit verify audit.jsonl        # exit 0 = intact, 1 = tampered
```

`--mapping` takes a preset (`langsmith` / `langfuse` / `openai`) or a
`TraceMapping` JSON file; `--extractor TOOL=module:function` registers an evidence
extractor (uvicorn-style import). Every run prints a **coverage** section that
names any *unclassified* tool — a call reached in the trace that no `--action` or
`--extractor` accounts for — so residual risk is surfaced, never silently missed.
`--now` injects the evaluation time for reproducible runs.
