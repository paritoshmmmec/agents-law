"""CLI tests: replay and audit-verify over the real gate, plus argument errors."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from evidence_gate import (
    EvidenceItem,
    EvidenceManifest,
    EvidenceSource,
    Gate,
    PolicySet,
    ProposedAction,
)
from evidence_gate.audit import AuditLog
from evidence_gate.cli import main
from evidence_gate.trace import ToolCall

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


# Referenced by --extractor as `tests.test_cli:optin_extractor`; must be module-level.
def optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    result = call.result
    if not result or not result.get("found"):
        return []
    return [
        EvidenceItem(
            id=f"optin-{call.call_id}",
            claim="marketing opt-in",
            key="marketing.opt_in",
            value=result["value"],
            source=EvidenceSource.TOOL_RESULT,
            source_id=call.call_id,
            observed_at=call.observed_at,
            observed=True,
        )
    ]


def _write_trace(tmp_path: Path) -> Path:
    trace = [
        {"name": "get_optin", "id": "c42", "ts": "2026-05-06T00:00:00+00:00",
         "data": {"inputs": {"contact_id": 42}, "output": {"found": True, "value": True}}},
        {"name": "send_marketing", "id": "s42", "ts": "2026-07-05T00:00:00+00:00",
         "data": {"inputs": {"contact_id": 42}, "output": None}},
        {"name": "get_optin", "id": "c99", "ts": "2026-07-05T00:00:00+00:00",
         "data": {"inputs": {"contact_id": 99}, "output": {"found": False}}},
        {"name": "send_marketing", "id": "s99", "ts": "2026-07-05T00:00:00+00:00",
         "data": {"inputs": {"contact_id": 99}, "output": None}},
    ]
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace))
    return path


def _write_mapping(tmp_path: Path) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(
        {"tool": "name", "call_id": "id", "observed_at": "ts",
         "args": "data.inputs", "result": "data.output"}
    ))
    return path


# --- replay ----------------------------------------------------------------
def test_replay_reaches_allow_and_block(tmp_path, capsys):
    code = main([
        "replay", str(_write_trace(tmp_path)),
        "--policy", "policies",
        "--mapping", str(_write_mapping(tmp_path)),
        "--action", "send_*=marketing.send_sequence",
        "--extractor", "get_optin=tests.test_cli:optin_extractor",
        "--now", "2026-07-05T00:00:00+00:00",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "s42  ALLOW" in out
    assert "s99  BLOCK" in out
    assert "ALLOW=1" in out and "BLOCK=1" in out
    # coverage section: get_optin recognized, send_marketing gated, nothing residual
    assert "Coverage:" in out
    assert "send_marketing" in out
    assert "unclassified        : (none)" in out


def test_replay_reports_residual_risk(tmp_path, capsys):
    trace = json.loads(_write_trace(tmp_path).read_text())
    trace.append({"name": "wire_transfer", "id": "w1", "ts": "2026-07-05T00:00:00+00:00",
                  "data": {"inputs": {"amount": 9000}, "output": None}})
    trace_path = tmp_path / "trace2.json"
    trace_path.write_text(json.dumps(trace))
    code = main([
        "replay", str(trace_path),
        "--policy", "policies",
        "--mapping", str(_write_mapping(tmp_path)),
        "--action", "send_*=marketing.send_sequence",
        "--extractor", "get_optin=tests.test_cli:optin_extractor",
        "--now", "2026-07-05T00:00:00+00:00",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "UNCLASSIFIED" in out
    assert "wire_transfer (x1)" in out


def test_replay_accepts_preset_name(tmp_path, capsys):
    # OpenAI preset expects different fields; our trace won't match, so every
    # record is skipped and surfaced — proving preset resolution works and that
    # unmapped records are never guessed at.
    code = main([
        "replay", str(_write_trace(tmp_path)),
        "--policy", "policies",
        "--mapping", "openai",
        "--action", "send_*=marketing.send_sequence",
        "--now", "2026-07-05T00:00:00+00:00",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "skipped 4" in out


def test_replay_bad_mapping_is_user_error(tmp_path, capsys):
    code = main([
        "replay", str(_write_trace(tmp_path)),
        "--policy", "policies", "--mapping", "nope",
        "--action", "send_*=x",
    ])
    assert code == 2
    assert "neither a preset" in capsys.readouterr().err


def test_replay_bad_action_pair_is_user_error(tmp_path, capsys):
    code = main([
        "replay", str(_write_trace(tmp_path)),
        "--policy", "policies",
        "--mapping", str(_write_mapping(tmp_path)),
        "--action", "no-equals-sign",
    ])
    assert code == 2
    assert "GLOB=ACTION" in capsys.readouterr().err


# --- audit verify ----------------------------------------------------------
def _write_audit_log(path: Path) -> None:
    log = AuditLog(path=path)
    gate = Gate(PolicySet.from_dir("policies"), audit=log)
    man = EvidenceManifest(
        items=[EvidenceItem(id="o1", claim="opt-in", key="marketing.opt_in", value=True,
                            source=EvidenceSource.TOOL_RESULT, source_id="c1",
                            observed_at=NOW, observed=True)],
        compiled_at=NOW,
    )
    gate.check(ProposedAction(action="marketing.send_sequence", payload={"contact_id": 1},
                              actor="t", request_id="r1"), man, now=NOW)
    gate.check(ProposedAction(action="marketing.send_sequence", payload={},
                              actor="t", request_id="r2"),
               EvidenceManifest(items=[], compiled_at=NOW), now=NOW)


def test_audit_verify_clean_chain(tmp_path, capsys):
    log_path = tmp_path / "audit.jsonl"
    _write_audit_log(log_path)
    code = main(["audit", "verify", str(log_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "chain intact: True" in out


def test_audit_verify_detects_tampering(tmp_path, capsys):
    log_path = tmp_path / "audit.jsonl"
    _write_audit_log(log_path)
    tampered = log_path.read_text().replace("contact_id", "CONTACT_ID")
    log_path.write_text(tampered)
    code = main(["audit", "verify", str(log_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "chain intact: False" in out


def test_audit_verify_missing_file(capsys):
    code = main(["audit", "verify", "/tmp/does-not-exist-eg.jsonl"])
    assert code == 2
    assert "no such file" in capsys.readouterr().err


# --- policy lint -----------------------------------------------------------

_VALID_POLICY = """\
version: "v1"
action: "refund.issue"
rules:
  - id: approved
    requirements:
      - key: "refund.manager_approved"
        must_exist: true
        equals: true
    effect_on_fail: block
"""


def test_policy_lint_clean(tmp_path, capsys):
    f = tmp_path / "refund.yaml"
    f.write_text(_VALID_POLICY)
    code = main(["policy", "lint", str(f)])
    assert code == 0
    assert "clean" in capsys.readouterr().out


def test_policy_lint_reports_errors_nonzero(tmp_path, capsys):
    f = tmp_path / "bad.yaml"
    f.write_text("- not\n- a\n- mapping\n")
    code = main(["policy", "lint", str(f)])
    assert code == 1
    assert "ERROR" in capsys.readouterr().out


def test_policy_lint_missing_file(capsys):
    code = main(["policy", "lint", "/tmp/does-not-exist-eg.yaml"])
    assert code == 2
    assert "no such file" in capsys.readouterr().err


def test_policy_compile_missing_sop(capsys):
    code = main(["policy", "compile", "/tmp/does-not-exist-eg.txt"])
    assert code == 2
    assert "no such file" in capsys.readouterr().err
