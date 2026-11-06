"""A conversation with a warm prefix waits once for its account's hourly limit before it moves (item 18, a5).

When Retool answers 429 with the hourly managed-key limit, the conversation moves to another account, which cannot take the
signed history along and writes the whole prefix again: 78,852 tokens and a first token after 38 s (a5, request ddd7b74f), and
489,786 tokens after one more such 429 (8f8d8567, a5's Codex run on Opus). Retool sends no Retry-After for this limit, so
nobody knows how long it lasts; account_rate_limit_wait lets a request that has something to lose wait on the account and ask
once more (0, the default, moves it at once, as before). The account is neither parked nor excluded during the wait.
"""
import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

import apierr
import config
import journal
import orchestrator
from test_account_rate_limit import HOURLY_BODY, _events, _headers, hourly, retool, serve_ok, turn  # noqa: F401 - retool is a fixture
from test_admin import admin_relay  # noqa: F401 - a fixture
from test_docs_consistency import load_with
from test_native_context_reuse import run

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def pauses(monkeypatch):
    """The waits of the requests, recorded and not waited for."""
    waited = []

    async def instant(cancelled, seconds):
        waited.append(seconds)
        return False

    monkeypatch.setattr(orchestrator, "_pause", instant)
    return waited


def limited(times, hint=None):
    """An account that answers the hourly limit `times` times (with Retry-After `hint`), then serves."""
    left = {"times": times}

    def script(request_):
        if left["times"] > 0:
            left["times"] -= 1
            raise urllib.error.HTTPError(request_.full_url, 429, "Too Many Requests",
                                         _headers(retry_after=str(hint)) if hint else _headers(),
                                         io.BytesIO(HOURLY_BODY.encode()))
        return serve_ok(request_)

    return script


async def second_turn(retool, script, *, wait=30, prefix=50000, key="conv-1"):
    """A conversation that has been served on acc-a once (its prefix is `prefix` tokens), then a turn that meets `script`."""
    retool.runner.cfg["account_rate_limit_wait"] = wait
    await run(retool.runner, turn(key))
    session = retool.pool._conversations[key]
    session._last_prefix_tokens = prefix
    retool.posts.clear()
    retool.script["acc-a"] = script
    before = journal.JOURNAL.last_seq
    return await run(retool.runner, turn(key)), session, before


def answered(events):
    return {"type": "text", "delta": "OK"} in events and not any(event["type"] == "error" for event in events)


def test_a_warm_conversation_waits_for_its_limited_account_and_stays_on_it(retool, pauses):
    events, session, before = asyncio.run(second_turn(retool, limited(1)))
    assert retool.posts == ["acc-a", "acc-a"] and answered(events)
    assert pauses == [30.0]
    assert retool.a.unavailable is None  # not parked, so that nobody else is sent away either
    assert retool.pool._conversations["conv-1"] is session and session.backend is retool.a
    waits = _events("account.rate_limit_wait", before)
    assert len(waits) == 1 and waits[0]["request_id"] == "req-conv-1"
    assert waits[0]["data"]["seconds"] == 30.0 and waits[0]["data"]["retry_after"] is None
    lifted = _events("account.rate_limit_lifted", before)
    assert len(lifted) == 1 and lifted[0]["data"]["seconds"] == 30.0
    for kind in ("account.rate_limited", "account.failover", "account.migrate"):
        assert not _events(kind, before), kind
    assert not retool.pool._active


def test_a_retry_after_that_fits_is_waited_for_exactly(retool, pauses):
    events, _, before = asyncio.run(second_turn(retool, limited(1, hint=7)))
    assert retool.posts == ["acc-a", "acc-a"] and answered(events) and pauses == [7.0]
    assert _events("account.rate_limit_wait", before)[0]["data"]["retry_after"] == 7


def test_a_retry_after_longer_than_the_setting_moves_the_conversation_at_once(retool, pauses):
    events, session, before = asyncio.run(second_turn(retool, limited(1, hint=45)))
    assert retool.posts == ["acc-a", "acc-b"] and answered(events) and pauses == []
    assert (retool.a.unavailable["pause"], retool.a.unavailable["source"]) == (45.0, "retry_after")
    assert retool.pool._conversations["conv-1"].backend is retool.b
    assert not _events("account.rate_limit_wait", before) and len(_events("account.failover", before)) == 1


def test_a_second_limit_after_the_wait_takes_the_usual_way_and_the_wait_is_not_repeated(retool, pauses):
    events, _, before = asyncio.run(second_turn(retool, limited(5)))
    assert retool.posts == ["acc-a", "acc-a", "acc-b"] and answered(events)
    assert pauses == [30.0]
    assert retool.a.unavailable["reason"] == "rate_limited" and retool.pool._conversations["conv-1"].backend is retool.b
    assert len(_events("account.rate_limited", before)) == 1 and len(_events("account.failover", before)) == 1
    assert len(_events("account.migrate", before)) == 1 and not _events("account.rate_limit_lifted", before)  # as ever


def test_with_the_setting_off_which_is_the_default_the_conversation_moves_at_once(retool, pauses):
    assert config.DEFAULTS["account_rate_limit_wait"] == 0
    events, _, before = asyncio.run(second_turn(retool, limited(1), wait=0))
    assert retool.posts == ["acc-a", "acc-b"] and answered(events) and pauses == []
    assert not _events("account.rate_limit_wait", before) and len(_events("account.failover", before)) == 1


def test_a_conversation_with_a_small_prefix_does_not_wait(retool, pauses):
    events, _, before = asyncio.run(second_turn(retool, limited(1), prefix=orchestrator.LIMIT_WAIT_MIN_PREFIX - 1))
    assert retool.posts == ["acc-a", "acc-b"] and answered(events) and pauses == []
    assert not _events("account.rate_limit_wait", before)


def test_a_new_conversation_does_not_wait(retool, pauses):
    retool.runner.cfg["account_rate_limit_wait"] = 30
    retool.script["acc-a"] = hourly
    events = asyncio.run(run(retool.runner, turn()))
    assert retool.posts == ["acc-a", "acc-b"] and answered(events) and pauses == []


def test_a_client_that_goes_away_during_the_wait_is_not_retried_and_the_account_is_told(retool, monkeypatch):
    async def gone(cancelled, seconds):
        return True

    monkeypatch.setattr(orchestrator, "_pause", gone)
    events, _, before = asyncio.run(second_turn(retool, limited(1)))
    assert retool.posts == ["acc-a"]  # no second ask, no other account
    assert [event["error"].code for event in events if event["type"] == "error"] == ["rate_limit_error"]
    assert retool.a.unavailable["reason"] == "rate_limited"
    assert not _events("account.failover", before) and not _events("account.rate_limit_lifted", before)


def test_the_pause_returns_early_for_a_client_that_went_away_and_takes_no_thread():
    import threading
    import time

    async def scenario():
        cancelled = threading.Event()
        started = time.monotonic()
        full = await orchestrator._pause(cancelled, 0.3)
        waited = time.monotonic() - started
        cancelled.set()
        early = await orchestrator._pause(cancelled, 30)
        return full, waited, early, time.monotonic() - started

    full, waited, early, total = asyncio.run(scenario())
    assert full is False and waited >= 0.29
    assert early is True and total < 2


@pytest.mark.parametrize("setting,hint,prefix,expected", [
    (0, None, 50000, None), (None, None, 50000, None), (30, None, 50000, 30.0), (30, 7, 50000, 7.0), (30, 30, 50000, 30.0),
    (30, 31, 50000, None), (30, None, 7999, None), (30, None, 8000, 30.0), (30, None, 0, None), (30, None, None, None)])
def test_how_long_a_request_waits(retool, setting, hint, prefix, expected):
    retool.runner.cfg["account_rate_limit_wait"] = setting
    error = apierr.account_rate_limited("hourly limit", hint)
    assert retool.runner._limit_pause(error, SimpleNamespace(_last_prefix_tokens=prefix)) == expected


# --- the setting -----------------------------------------------------------------------------------------------------

def test_the_setting_is_validated_documented_and_live_in_the_admin(tmp_path, monkeypatch, admin_relay):  # noqa: F811
    assert load_with(tmp_path, monkeypatch)["account_rate_limit_wait"] == 0
    for good in (0, 1, 30, 120):
        assert load_with(tmp_path, monkeypatch, account_rate_limit_wait=good)["account_rate_limit_wait"] == good
    for bad in (-1, 121, True, 1.5, "soon"):
        with pytest.raises(apierr.ApiError, match="account_rate_limit_wait"):
            load_with(tmp_path, monkeypatch, account_rate_limit_wait=bad)
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["account_rate_limit_wait"] == config.DEFAULTS["account_rate_limit_wait"] and example["account_rate_limit_wait_note"]
    assert "| account_rate_limit_wait |" in (ROOT / "README.md").read_text(encoding="utf-8")
    status, body, _ = admin_relay.request("/admin/api/settings")
    assert body["schema"]["account_rate_limit_wait"]["restart_required"] is False
    status, body, _ = admin_relay.patch({"account_rate_limit_wait": 20})
    assert status == 200 and body["applied"] == ["account_rate_limit_wait"]
    assert admin_relay.cfg["account_rate_limit_wait"] == 20
    assert admin_relay.patch({"account_rate_limit_wait": 121})[0] == 400 and admin_relay.patch({"account_rate_limit_wait": -1})[0] == 400
