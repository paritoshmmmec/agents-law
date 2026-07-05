"""The deterministic policy engine.

`evaluate()` is a pure function of `(action, manifest, policy, now)`: no clock
reads, no randomness, no I/O. Identical inputs always yield an identical
`Decision`. This is the non-probabilistic boundary the whole system exists to
provide (DESIGN.md §5.5).

`now` is injected rather than read from the wall clock precisely so the "stale"
branch is reproducible in tests and in audit replay.
"""

from __future__ import annotations

from datetime import datetime

import operator

from evidence_gate.policy import Comparison, Policy, Requirement, Rule
from evidence_gate.schemas import (
    Decision,
    Effect,
    EvidenceItem,
    EvidenceManifest,
    RuleResult,
)

# Named, total functions — no `eval`, no expression language (DESIGN §5.1).
_COMPARATORS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
_RELATIONAL = {"<", "<=", ">", ">="}


def evaluate(
    action: str,
    manifest: EvidenceManifest,
    policy: Policy | None,
    now: datetime,
) -> Decision:
    """Evaluate a proposed action's evidence against its policy.

    `policy is None` means no rule governs this action — deny by default
    (DESIGN §5.4): an ungoverned sensitive action is a policy gap, not an
    implicit allow.
    """
    if policy is None:
        return _decision(
            action_request_id=_request_id(manifest),
            results=[
                RuleResult(
                    rule_id="__default__",
                    effect=Effect.BLOCK,
                    reason=f"no policy governs action {action!r} (deny by default)",
                )
            ],
            now=now,
            policy_version=None,
        )

    results = [_evaluate_rule(rule, manifest, now) for rule in policy.rules]
    return _decision(
        action_request_id=_request_id(manifest),
        results=results,
        now=now,
        policy_version=policy.version,
    )


def _evaluate_rule(rule: Rule, manifest: EvidenceManifest, now: datetime) -> RuleResult:
    """Evaluate one rule. Returns ALLOW if every check passes."""
    # Conflict rules are checked independently of the agent's declarations.
    for key in rule.forbid_conflicts_on:
        conflict = _find_conflict(manifest.by_key(key))
        if conflict is not None:
            a, b = conflict
            return RuleResult(
                rule_id=rule.id,
                effect=rule.effect_on_fail,
                reason=f"conflicting evidence on {key!r}: {a.value!r} vs {b.value!r}",
                evidence_refs=[a.id, b.id],
            )

    for req in rule.requirements:
        failure = _check_requirement(req, manifest, now)
        if failure is not None:
            reason, stale, refs = failure
            # A pure staleness failure routes to effect_on_stale when the rule
            # defines one; every other failure uses effect_on_fail.
            effect = (
                rule.effect_on_stale
                if stale and rule.effect_on_stale is not None
                else rule.effect_on_fail
            )
            return RuleResult(
                rule_id=rule.id, effect=effect, reason=reason, evidence_refs=refs
            )

    # Cross-key comparison (e.g. refund.amount <= order.total). Evaluated last,
    # after the per-key requirements have vetted the operands' provenance.
    if rule.compare is not None:
        failure = _check_comparison(rule.compare, manifest)
        if failure is not None:
            reason, refs = failure
            return RuleResult(
                rule_id=rule.id, effect=rule.effect_on_fail, reason=reason, evidence_refs=refs
            )

    return RuleResult(rule_id=rule.id, effect=Effect.ALLOW, reason="ok")


# A requirement check returns None on success, or (reason, is_stale, refs) on
# failure. `is_stale` is True only when the *sole* violated constraint is
# max_age, so the rule can route freshness failures differently.
_Failure = tuple[str, bool, list[str]]


def _check_requirement(
    req: Requirement, manifest: EvidenceManifest, now: datetime
) -> _Failure | None:
    items = manifest.by_key(req.key)

    # must_exist / missing evidence.
    if req.must_exist and not items:
        return (f"missing required evidence for {req.key!r}", False, [])

    # Constraints below only make sense when there is evidence to check. An
    # absent key with must_exist=False is not a violation.
    if not items:
        return None

    # A requirement is satisfied if ANY item for the key satisfies all of its
    # non-freshness constraints. We pick the best candidate, then judge its
    # freshness — so a fresh-but-wrong item can't mask a stale-but-right one,
    # and staleness is only reported when the value/source/etc. were otherwise
    # acceptable.
    qualifying: list[EvidenceItem] = []
    for item in items:
        if _non_freshness_reason(req, item) is None:
            qualifying.append(item)

    if not qualifying:
        # Report the first item's specific failure for a useful message.
        reason = _non_freshness_reason(req, items[0])
        assert reason is not None
        return (f"{req.key!r}: {reason}", False, [items[0].id])

    # Among qualifying items, accept if any is fresh enough.
    if req.max_age is not None:
        window = req.max_age.to_timedelta()
        fresh = [it for it in qualifying if (now - it.observed_at) <= window]
        if not fresh:
            newest = max(qualifying, key=lambda it: it.observed_at)
            age_days = (now - newest.observed_at).days
            return (
                f"{req.key!r}: stale evidence — newest is {age_days}d old, "
                f"limit {window.days}d",
                True,
                [newest.id],
            )

    return None


def _non_freshness_reason(req: Requirement, item: EvidenceItem) -> str | None:
    """Why `item` fails `req`, ignoring freshness. None if it passes them all."""
    if req.equals is not None and item.value != req.equals:
        return f"value {item.value!r} != required {req.equals!r}"
    if req.in_ is not None and item.value not in req.in_:
        return f"value {item.value!r} not in {req.in_!r}"
    if req.source_in is not None and item.source not in req.source_in:
        return f"unauthorized source {item.source.value!r}"
    if req.observed is not None and item.observed != req.observed:
        return "requires directly-observed evidence, got inferred"
    if req.min_confidence is not None and item.confidence < req.min_confidence:
        return f"confidence {item.confidence} below floor {req.min_confidence}"
    return None


def _representative(items: list[EvidenceItem]) -> EvidenceItem | None:
    """The item that speaks for a key in a comparison: newest observed one.

    Prefers directly-observed items over inferred ones, then most recent. This
    keeps a stale or inferred duplicate from silently setting the compared value.
    """
    if not items:
        return None
    observed = [it for it in items if it.observed]
    pool = observed or items
    return max(pool, key=lambda it: it.observed_at)


def _check_comparison(cmp: Comparison, manifest: EvidenceManifest) -> tuple[str, list[str]] | None:
    """Evaluate a cross-key/threshold comparison. None on pass, (reason, refs) on fail."""
    left = _representative(manifest.by_key(cmp.left_key))
    if left is None:
        return (f"comparison needs evidence for {cmp.left_key!r}, none present", [])

    refs = [left.id]
    if cmp.right_key is not None:
        right_item = _representative(manifest.by_key(cmp.right_key))
        if right_item is None:
            return (f"comparison needs evidence for {cmp.right_key!r}, none present", refs)
        right_val: object = right_item.value
        right_label = cmp.right_key
        refs.append(right_item.id)
    else:
        right_val = cmp.right_value
        right_label = repr(cmp.right_value)

    fn = _COMPARATORS[cmp.op]
    if cmp.op in _RELATIONAL and not (
        _is_number(left.value) and _is_number(right_val)
    ):
        return (
            f"comparison {cmp.left_key} {cmp.op} {right_label} needs numeric operands, "
            f"got {left.value!r} and {right_val!r}",
            refs,
        )

    if not fn(left.value, right_val):
        return (
            f"comparison failed: {cmp.left_key}={left.value!r} {cmp.op} {right_label}"
            + (f"={right_val!r}" if cmp.right_key is not None else ""),
            refs,
        )
    return None


def _is_number(v: object) -> bool:
    # bool is an int subclass; exclude it so True/False never sort as 1/0 here.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_conflict(
    items: list[EvidenceItem],
) -> tuple[EvidenceItem, EvidenceItem] | None:
    """Return the first pair of observed items whose values disagree."""
    observed = [it for it in items if it.observed]
    for i in range(len(observed)):
        for j in range(i + 1, len(observed)):
            if observed[i].value != observed[j].value:
                return (observed[i], observed[j])
    return None


def _decision(
    action_request_id: str,
    results: list[RuleResult],
    now: datetime,
    policy_version: str | None,
) -> Decision:
    """Aggregate rule results with most-restrictive-wins (DESIGN §5.4)."""
    effect = max(
        (r.effect for r in results),
        key=lambda e: e.severity,
        default=Effect.ALLOW,
    )
    return Decision(
        effect=effect,
        results=results,
        request_id=action_request_id,
        decided_at=now,
        policy_version=policy_version,
    )


def _request_id(manifest: EvidenceManifest) -> str:
    # The manifest alone doesn't carry the request id; the gate stamps it on the
    # decision it returns. Engine-level default keeps evaluate() usable in
    # isolation and in tests.
    return "unknown"
