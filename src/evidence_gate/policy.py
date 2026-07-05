"""Policy model + YAML loader.

Policies are declarative *data*, not code. A rule is a set of typed
`Requirement`s over the evidence for a key; the engine (engine.py) checks them
with named, individually-tested primitives — no expression language, no eval.
See DESIGN.md §5.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from evidence_gate.schemas import Effect, EvidenceSource

# The comparison operators the engine knows how to evaluate. Ordering/relational
# ops (`<`, `<=`, `>`, `>=`) require numeric operands; `==` / `!=` do not.
CompareOp = Literal["<", "<=", ">", ">=", "==", "!="]


class Duration(BaseModel):
    """A max-age window, authored as `{months: 12}` or `{days: 30}`.

    Months are treated as 30 days — good enough for staleness windows and keeps
    the arithmetic dependency-free and deterministic.
    """

    days: int = 0
    months: int = 0

    def to_timedelta(self) -> timedelta:
        return timedelta(days=self.days + self.months * 30)


class Requirement(BaseModel):
    """A constraint on the evidence recorded under one `key`.

    Every field is optional; only the ones set are checked. Each maps to a
    named primitive in the engine (DESIGN §5.2). This flat shape is what keeps a
    future `compare_keys` cross-key primitive additive rather than a rewrite.
    """

    key: str

    must_exist: bool = False  # at least one item for this key
    equals: Any = None  # value must equal this (checked when set)
    in_: list[Any] | None = Field(default=None, alias="in")  # value in set
    source_in: list[EvidenceSource] | None = None  # allowed provenance
    observed: bool | None = None  # require directly-observed evidence
    max_age: Duration | None = None  # freshness window
    min_confidence: float | None = None  # confidence floor

    model_config = {"populate_by_name": True}


class Comparison(BaseModel):
    """A constraint relating one evidence key's value to another key or a literal.

    This is the cross-key primitive (DESIGN §12.4): the constraint the fixed
    per-key `Requirement` set cannot express, e.g. `refund.amount <= order.total`.
    Exactly one of `right_key` / `right_value` is set — comparing a key against
    another key, or against an authored literal threshold.

    The comparison is evaluated over the *representative* value for each key
    (the newest qualifying observed item), so it composes with the same evidence
    the requirements already vet. A missing operand fails the rule rather than
    silently passing — an absent left/right value is treated as unprovable.
    """

    left_key: str
    op: CompareOp
    right_key: str | None = None
    right_value: Any = None

    @model_validator(mode="after")
    def _exactly_one_rhs(self) -> Comparison:
        has_key = self.right_key is not None
        has_value = self.right_value is not None
        if has_key == has_value:
            raise ValueError(
                "comparison needs exactly one of right_key / right_value "
                f"(left_key={self.left_key!r})"
            )
        return self


class Rule(BaseModel):
    """One rule governing an action.

    A rule checks `Requirement`s for a key, forbids conflicting evidence on a set
    of keys, or asserts a cross-key `Comparison`. The effects fired on failure
    are explicit, so the four failure modes (missing/stale/conflicting/
    unauthorized) each land on a deliberate verdict.
    """

    id: str
    description: str = ""

    requirements: list[Requirement] = Field(default_factory=list)
    forbid_conflicts_on: list[str] = Field(default_factory=list)
    compare: Comparison | None = None

    # `effect_on_fail` covers missing / wrong-value / unauthorized-source and a
    # failed `compare`. `effect_on_stale` is used specifically when only the
    # `max_age` check fails, letting "stale" route to REVIEW while a missing fact
    # hard-BLOCKs.
    effect_on_fail: Effect = Effect.BLOCK
    effect_on_stale: Effect | None = None

    @model_validator(mode="after")
    def _needs_a_check(self) -> Rule:
        if not self.requirements and not self.forbid_conflicts_on and self.compare is None:
            raise ValueError(
                f"rule {self.id!r} has no requirements, forbid_conflicts_on, or compare"
            )
        return self


class Policy(BaseModel):
    """A versioned rule pack for a single action."""

    version: str
    action: str
    rules: list[Rule] = Field(default_factory=list)


class PolicySet(BaseModel):
    """All policies loaded for a deployment, indexed by action id."""

    policies: dict[str, Policy] = Field(default_factory=dict)

    def get(self, action: str) -> Policy | None:
        return self.policies.get(action)

    @classmethod
    def from_dir(cls, path: str | Path) -> PolicySet:
        """Load every `*.yaml` policy under a directory."""
        policies: dict[str, Policy] = {}
        for file in sorted(Path(path).glob("*.yaml")):
            policy = _load_policy_file(file)
            if policy.action in policies:
                raise ValueError(
                    f"duplicate policy for action {policy.action!r} "
                    f"(second definition in {file})"
                )
            policies[policy.action] = policy
        return cls(policies=policies)


def _load_policy_file(file: Path) -> Policy:
    data = yaml.safe_load(file.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"policy file {file} must contain a YAML mapping")
    return Policy.model_validate(data)
