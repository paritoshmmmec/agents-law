"""Engine tests: the four failure modes, aggregation, and determinism."""

from __future__ import annotations

from datetime import timedelta

import pytest

from evidence_gate.engine import evaluate
from evidence_gate.schemas import EvidenceSource, Effect

from tests.conftest import manifest, opt_in

ACTION = "marketing.send_sequence"


@pytest.fixture
def policy(policies):
    return policies.get(ACTION)


# --- the marketing tripwire, one case per failure mode (DESIGN §10) ----------


def test_happy_path_allows(policy, now):
    d = evaluate(ACTION, manifest(opt_in(60)), policy, now)
    assert d.effect is Effect.ALLOW


def test_missing_evidence_blocks(policy, now):
    d = evaluate(ACTION, manifest(), policy, now)
    assert d.effect is Effect.BLOCK
    assert "missing" in d.results[0].reason


def test_stale_evidence_reviews(policy, now):
    d = evaluate(ACTION, manifest(opt_in(425)), policy, now)
    assert d.effect is Effect.REVIEW
    assert "stale" in d.results[0].reason


def test_inferred_source_blocks(policy, now):
    d = evaluate(
        ACTION,
        manifest(opt_in(60, source=EvidenceSource.INFERENCE, observed=False)),
        policy,
        now,
    )
    assert d.effect is Effect.BLOCK


def test_conflicting_evidence_reviews(policy, now):
    m = manifest(
        opt_in(60, value=True, item_id="a"),
        opt_in(30, value=False, source=EvidenceSource.RETRIEVAL, item_id="b"),
    )
    d = evaluate(ACTION, m, policy, now)
    assert d.effect is Effect.REVIEW
    assert "conflict" in d.results[-1].reason.lower()


# --- specific primitive behaviour --------------------------------------------


def test_wrong_value_blocks(policy, now):
    d = evaluate(ACTION, manifest(opt_in(60, value=False)), policy, now)
    assert d.effect is Effect.BLOCK


def test_low_confidence_blocks(policy, now):
    d = evaluate(ACTION, manifest(opt_in(60, confidence=0.5)), policy, now)
    assert d.effect is Effect.BLOCK


def test_boundary_age_is_fresh(policy, now):
    # Exactly at the 12-month (360-day) window is still allowed.
    d = evaluate(ACTION, manifest(opt_in(360)), policy, now)
    assert d.effect is Effect.ALLOW


def test_one_day_past_window_reviews(policy, now):
    d = evaluate(ACTION, manifest(opt_in(361)), policy, now)
    assert d.effect is Effect.REVIEW


def test_fresh_item_rescues_stale_duplicate(policy, now):
    # A stale item and a fresh item on the same key -> fresh one qualifies.
    m = manifest(opt_in(425, item_id="old"), opt_in(60, item_id="new"))
    d = evaluate(ACTION, m, policy, now)
    assert d.effect is Effect.ALLOW


# --- aggregation and defaults ------------------------------------------------


def test_most_restrictive_wins(policy, now):
    # Missing evidence (block) beats any conflict/review outcome.
    d = evaluate(ACTION, manifest(), policy, now)
    assert d.effect is Effect.BLOCK


def test_ungoverned_action_denies_by_default(now):
    d = evaluate("unknown.action", manifest(opt_in(60)), None, now)
    assert d.effect is Effect.BLOCK
    assert "deny by default" in d.results[0].reason


# --- determinism contract ----------------------------------------------------


def test_same_inputs_same_decision(policy, now):
    m = manifest(opt_in(425))
    first = evaluate(ACTION, m, policy, now)
    for _ in range(5):
        again = evaluate(ACTION, m, policy, now)
        assert again.model_dump() == first.model_dump()


def test_now_controls_staleness(policy, now):
    m = manifest(opt_in(200))  # 200 days old
    fresh = evaluate(ACTION, m, policy, now)
    later = evaluate(ACTION, m, policy, now + timedelta(days=200))
    assert fresh.effect is Effect.ALLOW
    assert later.effect is Effect.REVIEW
