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
import json
from datetime import datetime, timezone
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


# --- Per-vendor presets ----------------------------------------------------
# The generic mapper already carries every vendor; these are the field paths
# for the three formats people actually export, so callers don't have to look
# them up. Each assumes one record == one tool call, already flattened to the
# vendor's per-observation shape (LangSmith Run / Langfuse Observation / one
# OpenAI tool_call). normalize() handles the format quirks each implies —
# epoch-second timestamps and JSON-string args/result — with no vendor code.

#: LangSmith `Run` objects with ``run_type="tool"`` (inputs/outputs are dicts,
#: ``start_time`` an ISO8601 string). Filter to tool runs before normalizing.
LANGSMITH = TraceMapping(
    tool="name", call_id="id", observed_at="start_time", args="inputs", result="outputs"
)

#: Langfuse ``Observation`` objects (``type="SPAN"``). The public read API often
#: returns ``input``/``output`` as JSON *strings* (unless ``parseIoAsJson`` is
#: set); normalize() JSON-decodes them transparently.
LANGFUSE = TraceMapping(
    tool="name", call_id="id", observed_at="startTime", args="input", result="output"
)

#: One OpenAI chat-completion ``tool_calls[]`` entry, flattened with the parent
#: completion's ``created`` (epoch seconds) alongside it —
#: ``{**tool_call, "created": completion.created}``. ``function.arguments`` is a
#: JSON string, which normalize() decodes. The tool *result* is not on the
#: completion (it arrives in a later ``role="tool"`` message), so ``result`` is
#: left unmapped; attach it from that message if you have it.
OPENAI = TraceMapping(
    tool="function.name", call_id="id", observed_at="created", args="function.arguments"
)


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


class CoverageReport(BaseModel):
    """Which tools in a trace the operator has actually accounted for.

    A tool in the trace is one of three things, and *unclassified* is the one that
    matters (vision §5.6 "coverage gaps"): a tool that matches no action pattern
    **and** has no evidence extractor is a path the operator has said nothing about
    — potentially a sensitive action reached through an unwrapped route. Surfacing
    it by name is the whole point; a silent miss reads as "covered" when it isn't.

    Counts are per distinct tool *name* (not per call), sorted for reproducibility.
    """

    gated: list[str] = Field(default_factory=list)  # tools matched to an action
    recognized_evidence: list[str] = Field(default_factory=list)  # have an extractor
    unclassified: list[str] = Field(default_factory=list)  # neither — residual risk
    call_counts: dict[str, int] = Field(default_factory=dict)  # tool -> #calls in trace

    @property
    def has_residual_risk(self) -> bool:
        return bool(self.unclassified)


_MISSING = object()


def _resolve(record: dict[str, Any], path: str) -> Any:
    """Walk a dotted `path` through nested dicts; `_MISSING` if any hop is absent."""
    cur: Any = record
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _parse_timestamp(raw: Any) -> datetime:
    """Coerce a trace timestamp to a datetime, or raise ValueError.

    Accepts a `datetime`, an ISO8601 string (LangSmith/Langfuse), or an
    int/float of Unix **seconds** (OpenAI's `created`). Epoch values are read as
    UTC so replay stays reproducible regardless of the host timezone.
    """
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, bool):  # bool is an int subclass — never a timestamp
        raise ValueError(f"not a timestamp: {raw!r}")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, timezone.utc)
    return datetime.fromisoformat(str(raw))


def _decode_dict(raw: Any) -> dict[str, Any]:
    """Best-effort coerce an args value to a dict; {} if it isn't one.

    Vendors differ: LangSmith hands back a dict, OpenAI a JSON string
    (`function.arguments`), Langfuse either depending on `parseIoAsJson`. A
    string is JSON-decoded; anything that isn't a dict after that contributes
    no args (evidence stays opt-in — we never invent structure).
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _decode_result(raw: Any) -> Any:
    """Like `_decode_dict` for results, but preserve non-dict payloads.

    A result may legitimately be a scalar or list; only *JSON strings* are
    decoded (so a Langfuse `output` string becomes the object an extractor
    expects). A plain string that isn't JSON is passed through unchanged.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


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
            observed_at = _parse_timestamp(observed_raw)
        except (ValueError, OSError, OverflowError):
            result.skipped.append(f"record[{i}]: unparseable observed_at {observed_raw!r}")
            continue

        args = _resolve(record, mapping.args) if mapping.args else _MISSING
        res = _resolve(record, mapping.result) if mapping.result else _MISSING
        result.calls.append(
            ToolCall(
                tool=str(tool),
                args={} if args is _MISSING else _decode_dict(args),
                result=None if res is _MISSING else _decode_result(res),
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


def coverage(
    calls: list[ToolCall],
    *,
    action_mapping: dict[str, str],
    builder: ManifestBuilder | None = None,
) -> CoverageReport:
    """Classify every distinct tool in `calls` as gated / recognized-evidence / unclassified.

    Pure and side-effect-free — it runs no gate check, just the same
    `action_mapping` glob (`simulate`'s classifier) and the builder's registered
    extractors. A tool that matches neither is *unclassified*: a route the operator
    hasn't accounted for, surfaced by name rather than silently missed.

    `builder=None` means no extractors are registered, so only gated tools are
    recognized and everything else is residual risk — the honest default before any
    extractor is wired.
    """
    known_evidence = builder.registered_tools if builder is not None else frozenset()
    gated: set[str] = set()
    recognized: set[str] = set()
    unclassified: set[str] = set()
    counts: dict[str, int] = {}
    for call in calls:
        counts[call.tool] = counts.get(call.tool, 0) + 1
        if _match_action(call.tool, action_mapping) is not None:
            gated.add(call.tool)
        elif call.tool in known_evidence:
            recognized.add(call.tool)
        else:
            unclassified.add(call.tool)
    return CoverageReport(
        gated=sorted(gated),
        recognized_evidence=sorted(recognized),
        unclassified=sorted(unclassified),
        call_counts=counts,
    )
