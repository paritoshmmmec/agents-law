"""Offline policy-compiler tests.

The LLM is stubbed by a canned `Drafter`, so these exercise the load-bearing
parts — lint against the real engine schema, mandatory human approval, and the
activate → `PolicySet.from_dir` round-trip — with no network and no model client.
"""

from __future__ import annotations

import pytest

from evidence_gate import (
    ApprovalRequired,
    DraftError,
    PolicySet,
    draft_from_sop,
    lint_policy_yaml,
)
from evidence_gate.compiler import _strip_fences

from tests.conftest import NOW

GOOD_YAML = """\
version: "draft.1"
action: "refund.issue"
rules:
  - id: manager_approval
    description: "Refunds over threshold need a fresh, observed manager approval."
    requirements:
      - key: "refund.manager_approved"
        must_exist: true
        equals: true
        source_in: [tool_result]
        observed: true
        max_age: { days: 7 }
        min_confidence: 0.9
    effect_on_fail: block
    effect_on_stale: review
"""


def canned(yaml_text: str):
    """A `Drafter` that ignores the SOP and returns fixed YAML."""
    return lambda _sop: yaml_text


# --- lint -------------------------------------------------------------------


def test_lint_accepts_valid_policy():
    policy, report = lint_policy_yaml(GOOD_YAML)
    assert report.ok
    assert report.errors == []
    assert policy is not None
    assert policy.action == "refund.issue"


def test_lint_flags_non_mapping():
    policy, report = lint_policy_yaml("- just\n- a\n- list\n")
    assert policy is None
    assert not report.ok
    assert "mapping" in report.errors[0].message


def test_lint_flags_invalid_yaml():
    policy, report = lint_policy_yaml("version: 'unterminated\n")
    assert policy is None
    assert not report.ok
    assert report.errors[0].location == "(document)"


def test_lint_surfaces_schema_error_by_field():
    # A rule with no requirements/forbid/compare fails the Policy validator.
    bad = """\
version: "v1"
action: "x.y"
rules:
  - id: empty
    effect_on_fail: block
"""
    policy, report = lint_policy_yaml(bad)
    assert policy is None
    assert not report.ok
    assert any("empty" in f.message or "compare" in f.message for f in report.errors)


def test_lint_warns_on_empty_rules():
    policy, report = lint_policy_yaml('version: "v1"\naction: "x.y"\nrules: []\n')
    assert report.ok  # parses fine; deny-by-default is legal
    assert any("denied by default" in w.message for w in report.warnings)


def test_lint_warns_on_fail_open_rule():
    yaml_text = """\
version: "v1"
action: "x.y"
rules:
  - id: soft
    requirements:
      - key: "k"
        must_exist: true
    effect_on_fail: allow
"""
    _, report = lint_policy_yaml(yaml_text)
    assert report.ok
    assert any("fails open" in w.message for w in report.warnings)


def test_lint_warns_on_max_age_without_stale_route():
    yaml_text = """\
version: "v1"
action: "x.y"
rules:
  - id: fresh
    requirements:
      - key: "k"
        must_exist: true
        max_age: { days: 30 }
    effect_on_fail: block
"""
    _, report = lint_policy_yaml(yaml_text)
    assert any("effect_on_stale" in w.message for w in report.warnings)


# --- draft ------------------------------------------------------------------


def test_draft_from_sop_returns_unapproved():
    draft = draft_from_sop("Refunds over $100 need manager approval.", canned(GOOD_YAML))
    assert not draft.approved
    assert draft.policy is not None
    assert draft.lint.ok


def test_draft_rejects_empty_sop():
    with pytest.raises(DraftError):
        draft_from_sop("   ", canned(GOOD_YAML))


def test_draft_rejects_empty_drafter_output():
    with pytest.raises(DraftError):
        draft_from_sop("something", canned(""))


def test_bad_llm_output_becomes_a_draft_with_errors_not_an_exception():
    # A hallucinated draft fails *linting*, surfaced to the reviewer — it does
    # not blow up, and it is not activatable.
    draft = draft_from_sop("do a thing", canned("not: [a, valid, policy]"))
    assert not draft.lint.ok
    assert not draft.approved


# --- approval + activation (the safety gate) --------------------------------


def test_activate_refused_without_approval(tmp_path):
    draft = draft_from_sop("sop", canned(GOOD_YAML))
    with pytest.raises(ApprovalRequired):
        draft.activate(tmp_path)


def test_approve_refused_when_lint_has_errors():
    draft = draft_from_sop("sop", canned("not: [a, valid, policy]"))
    with pytest.raises(ApprovalRequired):
        draft.approve("ops@corp", NOW)


def test_approved_draft_activates_and_loads(tmp_path):
    draft = draft_from_sop("sop", canned(GOOD_YAML))
    draft.approve("ops@corp", NOW)
    assert draft.approved and draft.approved_by == "ops@corp"

    out = draft.activate(tmp_path)
    assert out.name == "refund.issue.yaml"

    # The activated file is a policy the engine can actually load and run.
    loaded = PolicySet.from_dir(tmp_path)
    policy = loaded.get("refund.issue")
    assert policy is not None
    assert policy.rules[0].id == "manager_approval"


# --- helpers ----------------------------------------------------------------


def test_strip_fences_removes_code_block():
    fenced = "```yaml\nversion: v1\n```"
    assert _strip_fences(fenced).strip() == "version: v1"


def test_strip_fences_passes_through_plain():
    assert _strip_fences("version: v1\n") == "version: v1\n"
