# Trace-to-Gate (replay recorded traces)

Point the gate at the tool-call log an agent *already* produced and see what it
*would have decided* — the onboarding hook, before wiring anything live.

```python
from evidence_gate import TraceMapping, normalize, simulate

calls = normalize(trace_records, TraceMapping(tool="name", call_id="id",
                                              observed_at="ts", result="data.output")).calls
reports = simulate(calls, gate=gate, builder=builder,
                   action_mapping={"send_*": "marketing.send_sequence"}, now=now)
# -> [SimReport(request_id=..., effect=ALLOW/REVIEW/BLOCK, executed=..., reasons=[...])]
```

`normalize` maps arbitrary vendor exports (dotted field paths) into the `ToolCall`
shape; a record missing a required field is *skipped and surfaced*, never guessed.
For the common vendors this is a one-liner — ship-ready `TraceMapping` presets
cover their per-observation shapes:

```python
from evidence_gate import LANGSMITH, LANGFUSE, OPENAI, normalize

calls = normalize(runs, LANGSMITH).calls        # or LANGFUSE, or OPENAI
```

`simulate` scopes evidence per turn and runs every sensitive call through the
untouched `gate.check()`. See `examples/trace_replay.py`.
