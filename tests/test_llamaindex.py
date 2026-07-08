"""LlamaIndex adapter tests: a gated FunctionTool blocks/allows a sensitive call.

Guarded by importorskip so the suite still runs without the `llamaindex` extra.
The gated tools are invoked directly (as an agent would via `.call()`), so no LLM
or workflow run is needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("llama_index.core")

from llama_index.core.tools import FunctionTool  # noqa: E402

from evidence_gate import (  # noqa: E402
    EvidenceItem,
    EvidenceSource,
    Gate,
    GateSession,
    LocalGatePort,
    ManifestBuilder,
    PolicySet,
    gate_llama_tools,
)
from evidence_gate.errors import ClearanceDenied  # noqa: E402
from evidence_gate.trace import ToolCall  # noqa: E402

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)
ACTION = "marketing.send_sequence"


def _optin_extractor(call: ToolCall) -> list[EvidenceItem]:
    result = call.result
    if not result or not result.get("found"):
        return []
    observed_at = result.get("observed_at", call.observed_at)
    if isinstance(observed_at, str):
        observed_at = datetime.fromisoformat(observed_at)
    return [
        EvidenceItem(
            id=f"optin-{call.call_id}",
            claim="marketing opt-in",
            key="marketing.opt_in",
            value=result["value"],
            source=EvidenceSource.TOOL_RESULT,
            source_id=call.call_id,
            observed_at=observed_at,
            observed=True,
        )
    ]


def _builder() -> ManifestBuilder:
    return ManifestBuilder().register("get_optin", _optin_extractor)


def _session() -> GateSession:
    port = LocalGatePort(Gate(PolicySet.from_dir("policies")), _builder())
    return GateSession(port, action_mapping={"send_*": ACTION}, now=NOW)


def _wire(days_ago: int | None):
    """Build a fresh get_optin/send_marketing pair sharing one session."""
    sent: list = []

    def get_optin(contact_id: int) -> dict:
        if days_ago is None:
            return {"found": False}
        return {
            "found": True,
            "value": True,
            "observed_at": (NOW - timedelta(days=days_ago)).isoformat(),
        }

    def send_marketing(contact_id: int) -> str:
        sent.append(contact_id)
        return "sent"

    tools = gate_llama_tools(
        [
            FunctionTool.from_defaults(fn=get_optin, name="get_optin"),
            FunctionTool.from_defaults(fn=send_marketing, name="send_marketing"),
        ],
        _session(),
    )
    by_name = {t.metadata.name: t for t in tools}
    return by_name["get_optin"], by_name["send_marketing"], sent


def _content(tool_output) -> str:
    return getattr(tool_output, "content", tool_output)


def test_fresh_optin_allows_send():
    get_optin, send_tool, sent = _wire(days_ago=60)
    get_optin.call(contact_id=42)
    assert _content(send_tool.call(contact_id=42)) == "sent"
    assert sent == [42]


def test_missing_optin_blocks_send():
    get_optin, send_tool, sent = _wire(days_ago=None)
    get_optin.call(contact_id=99)
    with pytest.raises(ClearanceDenied) as exc:
        send_tool.call(contact_id=99)
    assert exc.value.decision.effect.value == "block"
    assert sent == []


def test_stale_optin_reviews_and_blocks_send():
    get_optin, send_tool, sent = _wire(days_ago=425)
    get_optin.call(contact_id=77)
    with pytest.raises(ClearanceDenied) as exc:
        send_tool.call(contact_id=77)
    assert exc.value.decision.effect.value == "review"
    assert sent == []


def test_acall_also_enforces():
    # A sync-only tool used in an async workflow is adapted to async; acall must
    # enforce independently. FunctionTool.acall wraps our sync fn, so the same
    # gating applies.
    import asyncio

    get_optin, send_tool, sent = _wire(days_ago=None)

    async def run() -> None:
        await get_optin.acall(contact_id=99)
        with pytest.raises(ClearanceDenied):
            await send_tool.acall(contact_id=99)

    asyncio.run(run())
    assert sent == []
