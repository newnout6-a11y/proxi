"""Idle cache warmups stop for an account that cannot take background traffic; warmups inside a turn stay."""
from types import SimpleNamespace

import pytest

from accounts import idle_block
from keepwarm_harness import START, backend as warm_backend, drive, session as warm_session

STATES = {"disabled": {"enabled": False}, "exhausted": {"unavailable": {"reason": "exhausted", "available": 3.0}},
          "auth_dead": {"unavailable": {"reason": "auth_dead"}},
          # Reasons the dispatcher does not know yet: only `backend.unavailable` being set counts.
          "rate_limited": {"unavailable": {"reason": "rate_limited", "error": "hourly limit"}},
          "quarantined": {"unavailable": {"reason": "quarantined"}}}


def unavailable_backend(state):
    value = warm_backend()
    for key, item in STATES[state].items():
        if key == "enabled":
            value.cfg["enabled"] = item
        else:
            value.unavailable = item
    return value


@pytest.mark.parametrize("state", sorted(STATES))
def test_idle_conversation_on_an_unavailable_account_is_never_pinged(monkeypatch, state):
    target = warm_session(backend=unavailable_backend(state), _warm_at=0)
    clock, pings = drive(monkeypatch, target, seconds=1800)
    assert pings == []
    assert clock.now < START + 5, "the scheduler must stop at once instead of polling until the idle limit"


def test_the_same_idle_conversation_is_pinged_while_its_account_is_healthy(monkeypatch):
    target = warm_session(_warm_at=0)
    _, pings = drive(monkeypatch, target, seconds=60)
    assert len(pings) == 1


@pytest.mark.parametrize("state", sorted(STATES))
def test_a_ping_inside_a_running_turn_ignores_the_account_state(monkeypatch, state):
    target = warm_session(backend=unavailable_backend(state), _busy=True, _turn_started_at=START - 201, _turn_seq=1,
                          _turn_body={"model": "claude-opus-5-5"}, _warm_body=None)
    _, pings = drive(monkeypatch, target, seconds=30)
    assert len(pings) == 1


def test_an_account_that_becomes_unavailable_mid_cycle_stops_the_next_idle_ping(monkeypatch):
    target = warm_session(_warm_at=START - 300)
    flipped = []

    def on_tick(now):
        if now > START + 100 and not flipped:
            target.backend.unavailable = {"reason": "rate_limited", "error": "hourly limit"}
            flipped.append(now)

    clock, pings = drive(monkeypatch, target, seconds=1800, on_tick=on_tick)
    assert len(pings) == 1 and flipped
    assert clock.now < START + 110


def test_an_account_that_recovers_is_warmed_again(monkeypatch):
    target = warm_session(backend=unavailable_backend("rate_limited"), _warm_at=START - 300)
    clock, pings = drive(monkeypatch, target, seconds=10)
    assert pings == [] and clock.now - START < 2
    target.backend.unavailable = None
    clock, pings = drive(monkeypatch, target, seconds=10)
    assert len(pings) == 1


@pytest.mark.parametrize("reason", ["exhausted", "auth_dead", "rate_limited", "quarantined", "anything-else"])
def test_idle_block_passes_the_reason_through_without_a_list_of_reasons(reason):
    assert idle_block(SimpleNamespace(cfg={}, unavailable={"reason": reason, "until": 1.0})) == reason


def test_idle_block_names_why_an_account_gets_no_background_traffic():
    assert idle_block(None) is None
    assert idle_block(SimpleNamespace(cfg={})) is None
    assert idle_block(SimpleNamespace(cfg={}, unavailable=None)) is None
    assert idle_block(SimpleNamespace(cfg={"enabled": False})) == "disabled"
    assert idle_block(SimpleNamespace(cfg={}, unavailable={})) == "unavailable"
    assert idle_block(SimpleNamespace(cfg={}, unavailable={"reason": None})) == "unavailable"
    assert idle_block(SimpleNamespace(cfg={}, unavailable="blocked")) == "unavailable"
    assert idle_block(SimpleNamespace(cfg={"enabled": False}, unavailable={"reason": "exhausted"})) == "disabled"
