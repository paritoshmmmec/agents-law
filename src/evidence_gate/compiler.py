"""Offline policy compiler — SOP text → candidate rule pack (DESIGN §9, Author).

The vision's *Author* step: draft a YAML policy from a plain-language standard
operating procedure via an LLM, lint it against the real schema, and require a
human to approve it **before** it can activate. Three hard rules keep this from
weakening the runtime's determinism contract:

  1. **This is offline tooling only.** Nothing here runs on the gate's decision
     path. `gate.py` / `engine.py` never import this module; the runtime stays
     LLM-free (DESIGN §9, invariant "No LLM in the runtime decision path").
  2. **Approval is mandatory and never silent.** A `PolicyDraft` is inert. Only
     `activate()` writes YAML into the directory the runtime loads from, and it
     refuses unless the draft carries an approver — a drafted-but-unapproved
     policy cannot reach enforcement by any path here.
  3. **The LLM is pluggable and untrusted.** A `Drafter` is just
     `Callable[[str], str]` returning YAML text; the model's output is parsed and
     linted against `policy.Policy` before a human ever sees "approve." A bad or
     hallucinated draft fails linting, not enforcement.

The linter is the load-bearing safety net: it reuses the exact `Policy` schema
the engine consumes, so anything that lints clean is a policy the engine can run,
and anything that doesn't is surfaced by field and reason — authoring never
stalls on an opaque parse error (vision §5.6 top risk).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Protocol

import yaml
from pydantic import BaseModel, Field, ValidationError

from evidence_gate.policy import Policy

# A drafter turns SOP prose into candidate policy YAML. Deliberately the whole
# LLM contract — swap in `openai_drafter(...)`, a canned string for tests, or any
# other generator without this module importing a model client.
Drafter = Callable[[str], str]


class DraftError(Exception):
    """The drafter produced something that is not usable policy YAML at all."""


class ApprovalRequired(Exception):
    """Refused to activate a policy that no human has approved (never silent)."""


class LintFinding(BaseModel):
    """One linter observation, addressed to the human about to approve."""

    level: Literal["error", "warning"]
    location: str  # e.g. "action", "rules[0].effect_on_fail" — where to look
    message: str


class LintReport(BaseModel):
    """The verdict on a candidate policy: errors block activation, warnings don't."""

    findings: list[LintFinding] = Field(default_factory=list)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        """True iff nothing blocks activation (warnings are advisory)."""
        return not self.errors


def lint_policy_yaml(yaml_text: str) -> tuple[Policy | None, LintReport]:
    """Parse + validate candidate YAML, returning the `Policy` (if it parses) and
    a `LintReport`. Never raises on bad input — the point is to *report*.

    Errors mean the engine could not run this pack: it does not parse, is not a
    mapping, or fails the `Policy` schema. Warnings are shapes that parse but are
    probably authoring mistakes (a rule that fails *open*, an empty pack that
    silently denies, a staleness window with nowhere to route).
    """
    findings: list[LintFinding] = []

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        findings.append(LintFinding(level="error", location="(document)", message=f"not valid YAML: {exc}"))
        return None, LintReport(findings=findings)

    if not isinstance(data, dict):
        got = type(data).__name__
        findings.append(
            LintFinding(
                level="error",
                location="(document)",
                message=f"a policy must be a YAML mapping, got {got}",
            )
        )
        return None, LintReport(findings=findings)

    try:
        policy = Policy.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            findings.append(LintFinding(level="error", location=loc, message=err["msg"]))
        return None, LintReport(findings=findings)

    findings.extend(_semantic_lints(policy))
    return policy, LintReport(findings=findings)


def _semantic_lints(policy: Policy) -> list[LintFinding]:
    """Shape checks the schema can't express — each maps to a likely mistake that
    would otherwise ship as a silent hole in enforcement."""
    findings: list[LintFinding] = []

    if not policy.rules:
        # Deny-by-default (engine §5.4) means an empty pack blocks the action
        # outright — legal, but almost never what an author drafting a rule wants.
        findings.append(
            LintFinding(
                level="warning",
                location="rules",
                message="policy has no rules; the action will be denied by default",
            )
        )

    for i, rule in enumerate(policy.rules):
        where = f"rules[{i}]"
        # A rule whose failure verdict is ALLOW enforces nothing — it can only
        # ever pass, which is a fail-open a reviewer should see spelled out.
        if rule.effect_on_fail.value == "allow":
            findings.append(
                LintFinding(
                    level="warning",
                    location=f"{where}.effect_on_fail",
                    message=f"rule {rule.id!r} fails open (effect_on_fail=allow); it enforces nothing on failure",
                )
            )
        # A freshness window with no stale route collapses staleness into the
        # hard-fail verdict — often fine, but worth confirming it was intended.
        for j, req in enumerate(rule.requirements):
            if req.max_age is not None and rule.effect_on_stale is None:
                findings.append(
                    LintFinding(
                        level="warning",
                        location=f"{where}.requirements[{j}].max_age",
                        message=(
                            f"rule {rule.id!r} sets max_age but no effect_on_stale; "
                            f"a stale value routes to effect_on_fail ({rule.effect_on_fail.value})"
                        ),
                    )
                )
            if req.min_confidence is not None and not 0.0 <= req.min_confidence <= 1.0:
                findings.append(
                    LintFinding(
                        level="error",
                        location=f"{where}.requirements[{j}].min_confidence",
                        message=f"min_confidence {req.min_confidence} is outside [0.0, 1.0]",
                    )
                )

    return findings


class PolicyDraft(BaseModel):
    """A candidate policy pending human approval. Inert until `approve()` is called.

    Holds everything a reviewer needs — the SOP it came from, the exact YAML the
    LLM produced, the parsed `Policy` (present iff it linted clean of errors), and
    the lint report — plus the approval stamp. `activate()` is the only path to
    the runtime, and it checks `approved_by`.
    """

    source_sop: str
    yaml_text: str
    policy: Policy | None
    lint: LintReport
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.approved_by is not None

    def approve(self, approver: str, now: datetime) -> PolicyDraft:
        """Record human sign-off. Refuses a draft with blocking lint errors —
        approval attests the pack is safe, so an un-runnable pack can't be signed."""
        if not self.lint.ok:
            raise ApprovalRequired(
                f"cannot approve a draft with {len(self.lint.errors)} lint error(s); fix them first"
            )
        self.approved_by = approver
        self.approved_at = now
        return self

    def activate(self, policy_dir: str | Path) -> Path:
        """Write the approved YAML into `policy_dir` where the runtime loads it.

        The single gate between an LLM's draft and live enforcement: it raises
        `ApprovalRequired` unless a human has approved, so activation is never
        silent (DESIGN §9). The file is named for the action so
        `PolicySet.from_dir` picks it up.
        """
        if not self.approved:
            raise ApprovalRequired(
                "policy has not been approved by a human; refusing to activate (DESIGN §9)"
            )
        assert self.policy is not None  # guaranteed: approve() requires lint.ok
        out = Path(policy_dir) / f"{self.policy.action}.yaml"
        out.write_text(self.yaml_text)
        return out


def draft_from_sop(sop_text: str, drafter: Drafter) -> PolicyDraft:
    """Draft a candidate policy from SOP text, lint it, and return it *unapproved*.

    The returned draft cannot activate until a human calls `approve()`. If the
    drafter returns nothing usable as text at all, that's a `DraftError` (the
    generator misbehaved); malformed *policy* content is not — it comes back as a
    draft carrying lint errors, which is exactly what the reviewer should see.
    """
    if not sop_text.strip():
        raise DraftError("empty SOP text; nothing to draft from")

    yaml_text = drafter(sop_text)
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise DraftError("drafter returned no policy text")

    policy, lint = lint_policy_yaml(yaml_text)
    return PolicyDraft(source_sop=sop_text, yaml_text=yaml_text, policy=policy, lint=lint)


# --- optional LLM drafter --------------------------------------------------
#
# Kept out of the module's import-time dependency surface: the compiler core is
# stdlib + pydantic + yaml, so `lint`/`activate` work with no model client
# installed. This factory lazily builds an OpenAI-compatible drafter, mirroring
# examples/llm_agent.py, only when a real compile is requested.

_SYSTEM_PROMPT = """\
You translate a plain-language standard operating procedure (SOP) into a single \
Evidence Gate policy, expressed as YAML. Output ONLY the YAML — no prose, no \
code fences.

The schema:
  version: a string version tag (use the SOP's date or "draft.1")
  action:  the canonical action id the policy governs, e.g. "refund.issue"
  rules:   a list; each rule has:
    id:          short snake_case identifier
    description: one sentence
    requirements: list of constraints on evidence keys, each with:
        key: str (required)
        and any of: must_exist(bool), equals(any), in(list), source_in(list of
        [tool_result, retrieval, user_input, memory, inference]),
        observed(bool), max_age({months: N} or {days: N}), min_confidence(0..1)
    forbid_conflicts_on: optional list of keys that must not disagree
    compare: optional cross-key check {left_key, op(<,<=,>,>=,==,!=), and exactly
        one of right_key or right_value}
    effect_on_fail:  one of allow, restrict, review, block (default block)
    effect_on_stale: optional; used only when a max_age check fails

Prefer block for missing/unauthorized evidence and review for stale evidence. \
A rule must have at least one of requirements / forbid_conflicts_on / compare.\
"""


def openai_drafter(
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Drafter:
    """Build a `Drafter` backed by an OpenAI-compatible endpoint.

    Reads `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` (falling back to
    `OPENROUTER_API_KEY`) like `examples/llm_agent.py`, so a `.env` already set up
    for the demo agent drives the compiler too. Import of `openai` is lazy — the
    rest of this module needs no model client.
    """
    import os

    from openai import OpenAI

    model = model or os.environ.get("LLM_MODEL", "z-ai/glm-4.7")
    base_url = base_url or os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise DraftError("no API key; set LLM_API_KEY or OPENROUTER_API_KEY to compile")

    client = OpenAI(base_url=base_url, api_key=api_key)

    def drafter(sop_text: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": sop_text},
            ],
            temperature=0,
        )
        return _strip_fences(resp.choices[0].message.content or "")

    return drafter


def _strip_fences(text: str) -> str:
    """Tolerate a model that wraps YAML in ```yaml fences despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # drop opening ``` / ```yaml
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip() + "\n"
    return text
