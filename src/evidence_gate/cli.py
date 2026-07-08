"""`evidence-gate` — the console entrypoint (the Phase-1 "CLI" surface).

Thin plumbing over the existing seams, no new core logic and no LLM:

  * ``evidence-gate replay TRACE --policy DIR --mapping M --action G=A``
    normalizes a recorded trace and runs every sensitive call through the
    untouched ``gate.check()`` — the Diagnose output (allow/restrict/review/block
    per call, with reasons), before wiring anything live.
  * ``evidence-gate audit verify LOG.jsonl`` recomputes a hash-chained audit log
    written by ``AuditLog(path=...)`` and reports whether the chain is intact.

Everything the engine touches (`now`) is read once at this boundary and injected,
so a run is reproducible given ``--now``; the engine itself never reads a clock.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from evidence_gate.audit import AuditLog, AuditRecord
from evidence_gate.gate import Gate
from evidence_gate.policy import PolicySet
from evidence_gate.signing import Signer
from evidence_gate.schemas import EvidenceItem
from evidence_gate.trace import ManifestBuilder, ToolCall
from evidence_gate.trace_adapters import (
    LANGFUSE,
    LANGSMITH,
    OPENAI,
    SimReport,
    TraceMapping,
    coverage,
    normalize,
    simulate,
)

_PRESETS: dict[str, TraceMapping] = {
    "langsmith": LANGSMITH,
    "langfuse": LANGFUSE,
    "openai": OPENAI,
}


class CLIError(Exception):
    """A user-facing error; printed to stderr with a non-zero exit, no traceback."""


# --- shared loaders --------------------------------------------------------


def _load_records(path: Path) -> list[dict[str, Any]]:
    """Read a trace file: a JSON array, or one JSON object per line (.jsonl)."""
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if not isinstance(data, list):
        raise CLIError(
            f"{path}: expected a JSON array of trace records "
            f"(or a .jsonl file), got {type(data).__name__}"
        )
    return data


def _resolve_mapping(spec: str) -> TraceMapping:
    """A preset name (langsmith/langfuse/openai) or a path to a TraceMapping JSON."""
    key = spec.lower()
    if key in _PRESETS:
        return _PRESETS[key]
    path = Path(spec)
    if path.is_file():
        return TraceMapping(**json.loads(path.read_text()))
    raise CLIError(
        f"--mapping {spec!r} is neither a preset "
        f"({', '.join(sorted(_PRESETS))}) nor an existing JSON file"
    )


def _parse_action_mapping(pairs: list[str]) -> dict[str, str]:
    """``--action 'send_*=marketing.send_sequence'`` (repeatable) -> {glob: action}."""
    mapping: dict[str, str] = {}
    for pair in pairs:
        glob, sep, action = pair.partition("=")
        if not sep or not glob or not action:
            raise CLIError(f"--action expects GLOB=ACTION, got {pair!r}")
        mapping[glob] = action
    return mapping


def _load_callable(ref: str) -> Callable[[ToolCall], list[EvidenceItem]]:
    """Import a ``module:function`` reference (uvicorn-style)."""
    module_name, sep, attr = ref.partition(":")
    if not sep or not attr:
        raise CLIError(f"expected 'module:function', got {ref!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise CLIError(f"cannot import module {module_name!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise CLIError(f"module {module_name!r} has no attribute {attr!r}") from exc


def _build_builder(extractor_specs: list[str]) -> ManifestBuilder:
    """``--extractor 'get_optin=my.mod:optin_extractor'`` (repeatable)."""
    builder = ManifestBuilder()
    for spec in extractor_specs:
        tool, sep, ref = spec.partition("=")
        if not sep or not tool or not ref:
            raise CLIError(f"--extractor expects TOOL=module:function, got {spec!r}")
        builder.register(tool, _load_callable(ref))
    return builder


def _parse_now(raw: str | None) -> datetime:
    """`--now` ISO8601 (injected for reproducibility), else current UTC."""
    if raw is None:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CLIError(f"--now {raw!r} is not ISO8601: {exc}") from exc


# --- subcommand: replay ----------------------------------------------------


def _format_reports(reports: list[SimReport]) -> str:
    lines = []
    for r in reports:
        line = f"  {r.request_id:>8}  {r.effect.value.upper():<8} executed={str(r.executed).lower()}"
        if r.reasons:
            line += f"  — {'; '.join(r.reasons)}"
        lines.append(line)
    return "\n".join(lines)


def cmd_replay(args: argparse.Namespace) -> int:
    records = _load_records(Path(args.trace))
    mapping = _resolve_mapping(args.mapping)
    action_mapping = _parse_action_mapping(args.action)
    builder = _build_builder(args.extractor)
    now = _parse_now(args.now)
    gate = Gate(PolicySet.from_dir(args.policy))

    norm = normalize(records, mapping)
    reports = simulate(
        norm.calls,
        gate=gate,
        builder=builder,
        action_mapping=action_mapping,
        now=now,
    )

    print(f"Normalized {len(norm.calls)} tool call(s); skipped {len(norm.skipped)}.")
    for reason in norm.skipped:
        print(f"  ! {reason}")

    if reports:
        print(f"\n{len(reports)} sensitive call(s) replayed through the gate:\n")
        print(_format_reports(reports))
    else:
        print("\nNo calls matched a --action pattern; nothing was gated.")

    tally: dict[str, int] = {}
    for r in reports:
        tally[r.effect.value] = tally.get(r.effect.value, 0) + 1
    if tally:
        summary = "  ".join(f"{k.upper()}={v}" for k, v in sorted(tally.items()))
        print(f"\nSummary: {summary}")

    cov = coverage(norm.calls, action_mapping=action_mapping, builder=builder)
    print("\nCoverage:")
    print(f"  gated               : {', '.join(cov.gated) or '(none)'}")
    print(f"  recognized evidence : {', '.join(cov.recognized_evidence) or '(none)'}")
    if cov.has_residual_risk:
        residual = ", ".join(f"{t} (x{cov.call_counts[t]})" for t in cov.unclassified)
        print(f"  ! UNCLASSIFIED      : {residual}")
        print("  ^ tools reached in the trace that no --action or --extractor accounts for.")
    else:
        print("  unclassified        : (none) — every tool in the trace is accounted for")
    # Residual risk is a warning, not a failure: replay is diagnostic, not enforcing.
    return 0


# --- subcommand: audit verify ----------------------------------------------


def cmd_audit_verify(args: argparse.Namespace) -> int:
    path = Path(args.log)
    if not path.is_file():
        raise CLIError(f"{path}: no such file")
    key = args.key.encode() if args.key else None
    log = AuditLog(signer=Signer(key))
    for i, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        try:
            log.records.append(AuditRecord.model_validate_json(line))
        except ValueError as exc:
            raise CLIError(f"{path} line {i + 1}: not a valid audit record: {exc}") from exc

    intact = log.verify()
    print(f"{path}: {len(log.records)} record(s), chain intact: {intact}")
    return 0 if intact else 1


# --- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evidence-gate",
        description="Deterministic evidence gate — replay traces and verify audit logs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser(
        "replay",
        help="replay a recorded trace through the gate and print the verdicts",
    )
    replay.add_argument("trace", help="trace file: a JSON array, or .jsonl")
    replay.add_argument("--policy", required=True, help="policy directory (PolicySet.from_dir)")
    replay.add_argument(
        "--mapping",
        required=True,
        help="trace mapping: a preset (langsmith/langfuse/openai) or a JSON file",
    )
    replay.add_argument(
        "--action",
        action="append",
        default=[],
        metavar="GLOB=ACTION",
        help="map a tool-name glob to an action id (repeatable), e.g. 'send_*=marketing.send_sequence'",
    )
    replay.add_argument(
        "--extractor",
        action="append",
        default=[],
        metavar="TOOL=module:function",
        help="register an evidence extractor for a tool (repeatable)",
    )
    replay.add_argument("--now", help="ISO8601 evaluation time (default: current UTC)")
    replay.set_defaults(func=cmd_replay)

    audit = sub.add_parser("audit", help="audit-log utilities")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    verify = audit_sub.add_parser("verify", help="recompute a JSONL audit chain")
    verify.add_argument("log", help="JSONL audit log written by AuditLog(path=...)")
    verify.add_argument("--key", help="HMAC key if the chain was signed (Signer key)")
    verify.set_defaults(func=cmd_audit_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
