"""Shared fixtures / builders for the test suite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from evidence_gate import EvidenceItem, EvidenceManifest, EvidenceSource, PolicySet

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def policies() -> PolicySet:
    return PolicySet.from_dir("policies")


def opt_in(
    days_ago: int,
    *,
    value: bool = True,
    source: EvidenceSource = EvidenceSource.TOOL_RESULT,
    observed: bool = True,
    confidence: float = 1.0,
    item_id: str = "e1",
) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        claim="opt-in",
        key="marketing.opt_in",
        value=value,
        source=source,
        source_id="crm:42",
        observed_at=NOW - timedelta(days=days_ago),
        observed=observed,
        confidence=confidence,
    )


def manifest(*items: EvidenceItem) -> EvidenceManifest:
    return EvidenceManifest(items=list(items), compiled_at=NOW)
