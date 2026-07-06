"""Client tests: fail-closed, ClearanceDenied, verified token, auto_instrument.

The client talks to a FastAPI `TestClient` injected as its transport (same
`.post`/`.get` shape as httpx), so these run without binding a socket. The
fail-closed test points a real httpx client at a dead port.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from evidence_gate import Gate, PolicySet, ProposedAction, Signer, Verifier
from evidence_gate.client import ClearanceDenied, GateUnreachable, RemoteGate

from tests.conftest import NOW, manifest, opt_in

ACTION = "marketing.send_sequence"
KEY = b"client-test-key"


@pytest.fixture
def gate() -> Gate:
    return Gate(PolicySet.from_dir("policies"))


@pytest.fixture
def remote(gate) -> RemoteGate:
    from evidence_gate.service import create_app

    transport = TestClient(create_app(gate, signer=Signer(KEY)))
    return RemoteGate("http://testserver", verifier=Verifier(KEY), transport=transport)


def _action(request_id="r1") -> ProposedAction:
    return ProposedAction(action=ACTION, payload={"c": 1}, actor="agent", request_id=request_id)


def test_allow_returns_verified_token(remote):
    result = remote.check(_action(), manifest=manifest(opt_in(60)), now=NOW)
    assert result.allowed
    assert result.token
    assert result.claims is not None  # verified on receipt
    assert result.claims["request_id"] == "r1"


def test_block_raises_clearance_denied(remote):
    with pytest.raises(ClearanceDenied) as exc:
        remote.check(_action("blocked"), manifest=manifest(), now=NOW)
    assert exc.value.request_id == "blocked"
    assert "marketing.opt_in" in exc.value.reason


def test_review_returns_pending_without_raising(remote):
    result = remote.check(_action(), manifest=manifest(opt_in(425)), now=NOW)
    assert result.effect.value == "review"
    assert result.review_ticket is not None
    assert not result.allowed


def test_unreachable_gate_fails_closed():
    # A real client pointed at a dead port must raise, never silently allow.
    dead = RemoteGate("http://127.0.0.1:59998", timeout=0.5)
    with pytest.raises(GateUnreachable):
        dead.check(_action(), manifest=manifest(opt_in(60)), now=NOW)


def test_enforce_decorator_runs_on_allow(remote):
    ran = {}

    @remote.enforce(action=ACTION)
    def send(payload, effect):
        ran["effect"] = effect
        return "sent"

    out = send({"c": 1}, manifest=manifest(opt_in(60)), request_id="r1", now=NOW)
    assert out == "sent"
    assert ran["effect"].value == "allow"


def test_enforce_decorator_does_not_run_on_block(remote):
    ran = {"called": False}

    @remote.enforce(action=ACTION)
    def send(payload, effect):
        ran["called"] = True
        return "sent"

    with pytest.raises(ClearanceDenied):
        send({"c": 1}, manifest=manifest(), request_id="r1", now=NOW)
    assert ran["called"] is False  # fail closed: tool never ran


def test_auto_instrument_wraps_by_name_pattern(remote):
    side_effects = []

    def stripe_refund(amount, *, effect=None):
        side_effects.append(amount)
        return {"refunded": amount}

    tools = {"stripe_refund": stripe_refund, "log_event": lambda: "noop"}
    remote.auto_instrument(tools, {"stripe_*": ACTION})

    # The matched tool is now gated; calling it BLOCKs with no evidence and the
    # underlying refund never runs.
    with pytest.raises(ClearanceDenied):
        tools["stripe_refund"](4000, manifest=manifest(), request_id="r1", now=NOW)
    assert side_effects == []

    # The unmatched tool is untouched.
    assert tools["log_event"]() == "noop"


def test_auto_instrument_allows_with_evidence(remote):
    calls = []

    def stripe_charge(amount, *, effect=None):
        calls.append((amount, effect))
        return "charged"

    tools = {"stripe_charge": stripe_charge}
    remote.auto_instrument(tools, {"stripe_*": ACTION})
    out = tools["stripe_charge"](50, manifest=manifest(opt_in(60)), request_id="r2", now=NOW)
    assert out == "charged"
    assert calls and calls[0][0] == 50
