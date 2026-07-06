"""Trace-to-Gate: replay recorded agent traces through the gate (COMPARISON.md §6 #4).

The onboarding hook. Point it at the tool-call log an agent *already* produced —
LangSmith / Langfuse run exports, an OpenAI chat transcript, a home-grown JSONL —
and see what the gate *would have decided*, before wiring anything live.

Two pieces, both plumbing over what already exists:

  * `normalize()` maps arbitrary trace-record dicts into `ToolCall`s (`trace.py`)
    via a declared `TraceMapping`. Field paths are dotted, so nested vendor exports
    (`data.inputs.contact_id`) map in without vendor-specific code. It guesses
    nothing: a record missing a required field is skipped and surfaced in
    `NormalizeResult.skipped`, never silently dropped.
  * `simulate()` walks the calls in order, feeding evidence tools to a
    `ManifestBuilder` and gating every call that matches a sensitive-action pattern
    against the evidence seen *so far* — exactly the flow `examples/llm_agent.py`
    runs live, replayed from a log. Every verdict comes from the untouched
    `gate.check()`; this module adds no evaluation logic.
"""

from __future__ import annotations

import fnmatch
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from evidence_gate.gate import Gate
from evidence_gate.schemas import Effect, ProposedAction
from evidence_gate.trace import ManifestBuilder, ToolCall


class TraceMapping(BaseModel):
    """Which fields of a trace record map to which `ToolCall` field.

    Values are dotted paths resolved against each record, e.g. `tool="name"` or
    `tool="data.tool"`. `args`/`result` are optional (a call may carry neither).
    """

    tool: str
    call_id: str
    observed_at: str
    args: str | None = None
    result: str | None = None


class NormalizeResult(BaseModel):
    """Outcome of `normalize`: the calls that mapped, and why any were skipped."""

    calls: list[ToolCall] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # human-readable skip reasons


class SimReport(BaseModel):
    """The verdict the gate would have returned for one sensitive call in a trace."""

    request_id: str
    action: str
    effect: Effect
    executed: bool
    reasons: list[str] = Field(default_factory=list)
    review_ticket: str | None = None


_MISSING = object()


def _resolve(record: dict[str, Any], path: str) -> Any:
    """Walk a dotted `path` through nested dicts; `_MISSING` if any hop is absent."""
    cur: Any = record
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def normalize(records: list[dict[str, Any]], mapping: TraceMapping) -> NormalizeResult:
    """Map raw trace records into `ToolCall`s via `mapping`.

    A record missing a required field (`tool` / `call_id` / `observed_at`) or with an
    unparseable timestamp is skipped and the reason recorded — never guessed at.
    """
    result = NormalizeResult()
    for i, record in enumerate(records):
        tool = _resolve(record, mapping.tool)
        call_id = _resolve(record, mapping.call_id)
        observed_raw = _resolve(record, mapping.observed_at)

        missing = [
            name
            for name, val in (("tool", tool), ("call_id", call_id), ("observed_at", observed_raw))
            if val is _MISSING
        ]
        if missing:
            result.skipped.append(f"record[{i}]: missing {', '.join(missing)}")
            continue

        try:
            observed_at = observed_raw if isinstance(observed_raw, datetime) else datetime.fromisoformat(str(observed_raw))
        except ValueError:
            result.skipped.append(f"record[{i}]: unparseable observed_at {observed_raw!r}")
            continue

        args = _resolve(record, mapping.args) if mapping.args else _MISSING
        res = _resolve(record, mapping.result) if mapping.result else _MISSING
        result.calls.append(
            ToolCall(
                tool=str(tool),
                args=args if args is not _MISSING and isinstance(args, dict) else {},
                result=None if res is _MISSING else res,
                call_id=str(call_id),
                observed_at=observed_at,
            )
        )
    return result


def _match_action(name: str, mapping: dict[str, str]) -> str | None:
    for pattern, action in mapping.items():
        if fnmatch.fnmatch(name, pattern):
            return action
    return None


def simulate(
    calls: list[ToolCall],
    *,
    gate: Gate,
    builder: ManifestBuilder,
    action_mapping: dict[str, str],
    now: datetime,
    actor: str = "trace-replay",
) -> list[SimReport]:
    """Replay `calls` through `gate`, gating each sensitive call against prior evidence.

    `action_mapping` is `{tool_glob: action_id}`. A call whose tool matches becomes a
    `gate.check()`; a non-matching call is evidence and feeds `builder`.

    Evidence is scoped to the *turn*: each sensitive call is judged on the evidence
    gathered since the previous sensitive call, then that evidence is consumed. This
    models an agent turn (gather → act → repeat) and keeps one action's evidence from
    bleeding into the next — a real risk when replaying a multi-subject trace.
    Deterministic in `calls` order and in the injected `now` — no wall-clock read here.
    """
    reports: list[SimReport] = []
    evidence: list[ToolCall] = []
    for call in calls:
        action_id = _match_action(call.tool, action_mapping)
        if action_id is None:
            evidence.append(call)  # evidence-bearing tool; gate reconstructs from these
            continue

        manifest = builder.build(evidence, now)
        action = ProposedAction(
            action=action_id,
            payload=call.args,
            actor=actor,
            request_id=call.call_id,
        )
        result = gate.check(action, manifest, now=now)
        reasons = [r.reason for r in result.decision.results if r.effect != Effect.ALLOW]
        reports.append(
            SimReport(
                request_id=call.call_id,
                action=action_id,
                effect=result.effect,
                executed=result.allowed,
                reasons=reasons,
                review_ticket=result.review_ticket,
            )
        )
        evidence = []  # turn boundary: this action's evidence is consumed
    return reports
