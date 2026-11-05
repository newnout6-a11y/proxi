"""Retool's hourly managed-key limit is an account quota: it parks the account and moves the turn."""
import asyncio
import http.client
import io
import json
import random
import threading
import time
import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest

import apierr
import admin
import config
import direct
import journal
import proxy
import upstream_attempts
from accounts import DEAD_AFTER, AccountDispatcher
from session import SessionPool
from test_account_dispatcher import Account, Clock, FakeManager, FakeRefresher, dispatcher
from test_admin import admin_relay  # noqa: F401 - fixture
from test_native_context_reuse import FIRST, Backend, request, run, setup
from test_review_fixes_pr19 import END, response, text
from test_review_fixes_proxy import FakeManager as BalanceManager
from upstream_attempts import AttemptBudget

MESSAGE = ("You have exceeded your hourly limit of managed API key usage. Please go to the Retool AI resource "
           "in the Resources page to configure your own API keys.")
HOURLY_BODY = json.dumps({"statusCode": 429, "error": "Too Many Requests", "message": MESSAGE})
SHORT_BODY = json.dumps({"statusCode": 429, "error": "Too Many Requests", "message": "Slow down, try again"})


def _headers(**values):
    message = Message()
    for name, value in values.items():
        message[name.replace("_", "-")] = value
    return message


# -- recognition ---------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    (MESSAGE, True),
    (MESSAGE.upper(), True),
    ("You have exceeded your HOURLY   limit\nof managed API key usage.", True),
    (HOURLY_BODY, True),
    ("Too Many Requests", False),
    ("Rate limit reached for requests", False),
    ("", False),
    (None, False),
])
def test_hourly_limit_text_is_recognised_without_regard_to_case_and_spacing(text, expected):
    assert apierr.is_hourly_limit(text) is expected


def test_http_429_with_the_hourly_text_is_flagged_as_an_account_quota():
    error = direct._api_error(429, HOURLY_BODY, _headers())
    assert (error.status, error.code) == (429, "rate_limit_error")
    assert apierr.is_account_quota(error) and error.retry_after is None
    assert error.message == MESSAGE


def test_a_non_json_429_body_with_the_hourly_text_is_flagged_too():
    assert apierr.is_account_quota(direct._api_error(429, "upstream: " + MESSAGE, None))


@pytest.mark.parametrize("header,expected", [("120", 120.0), ("1.5", 1.5), ("0", None), ("-5", None),
                                             ("soon", None), ("nan", None)])
def test_retry_after_header_is_kept_when_it_is_a_usable_number(header, expected):
    error = direct._api_error(429, HOURLY_BODY, _headers(retry_after=header))
    assert error.retry_after == expected and apierr.is_account_quota(error)


def test_retry_after_may_be_an_http_date():
    seconds = apierr.retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT")
    assert seconds is not None and seconds > 3600
    assert apierr.retry_after_seconds("Wed, 21 Oct 1999 07:28:00 GMT") is None


def test_an_ordinary_short_lived_429_is_not_an_account_quota():
    error = direct._api_error(429, SHORT_BODY, _headers(retry_after="5"))
    assert (error.status, error.code) == (429, "rate_limit_error")
    assert not apierr.is_account_quota(error) and error.retry_after is None


@pytest.mark.parametrize("status", [400, 402, 500, 502])
def test_only_a_429_can_be_an_account_quota(status):
    assert not apierr.is_account_quota(direct._api_error(status, HOURLY_BODY, _headers()))


def test_stream_error_events_carry_the_same_distinction():
    quota = direct._error_from_event({"event": "error", "statusCode": 429, "error": {"message": MESSAGE}})
    assert apierr.is_account_quota(quota) and quota.status == 429
    # Without a status the sentence itself still identifies the quota.
    assert apierr.is_account_quota(direct._error_from_event({"event": "error", "error": MESSAGE}))
    short = direct._error_from_event({"event": "error", "statusCode": 429, "error": "slow down"})
    assert short.status == 429 and not apierr.is_account_quota(short)
    other = direct._error_from_event({"event": "error", "statusCode": 500, "error": MESSAGE})
    assert other.status == 502 and not apierr.is_account_quota(other)


def _stream(tmp_path, monkeypatch, opener):
    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    session = SimpleNamespace(backend=SimpleNamespace(headers=lambda: {}, cfg={"base": "https://example.invalid"}),
                              _cancelled=False, _request_tag="req-429")
    cfg = {"upstream_error_dir": str(tmp_path), "turn_timeout": 2}
    body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}

    async def scenario():
        return [event async for event in direct._stream_body(cfg, session, body)]

    return asyncio.run(scenario())


@pytest.mark.parametrize("payload,quota", [(HOURLY_BODY, True), (SHORT_BODY, False)])
def test_the_transport_raises_the_flagged_error_after_one_attempt(tmp_path, monkeypatch, payload, quota):
    posts = []

    def opener(request, **_kwargs):
        posts.append(request)
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", _headers(retry_after="90"),
                                     io.BytesIO(payload.encode()))

    with pytest.raises(apierr.ApiError) as caught:
        _stream(tmp_path, monkeypatch, opener)
    error = caught.value
    assert len(posts) == 1 and error.upstream_attempts == 1 and error.upstream_status == 429
    assert apierr.is_account_quota(error) is quota
    assert error.retry_after == (90.0 if quota else None)


# -- parking the account -------------------------------------------------------------
def _events(kind, since):
    return [event for event in journal.JOURNAL.query(since=since) if event["kind"] == kind]


def test_a_marked_account_is_parked_for_the_configured_cooldown():
    account = Account("a")
    value, _ = dispatcher([account], account_rate_limit_cooldown=600)
    started = time.time()
    until = value.mark_rate_limited(account)
    assert account.unavailable["reason"] == "rate_limited"
    assert (account.unavailable["pause"], account.unavailable["source"]) == (600.0, "cooldown")
    assert started + 600 <= until <= time.time() + 600
    row = value.status()[0]
    assert (row["state"], row["rate_limited_until"]) == ("rate_limited", until)


def test_the_default_pause_is_fifteen_minutes():
    account = Account("a")
    value, _ = dispatcher([account])
    value.mark_rate_limited(account)
    assert account.unavailable["pause"] == 900.0
    assert config.DEFAULTS["account_rate_limit_cooldown"] == 900


@pytest.mark.parametrize("hint,pause", [(120, 120.0), (45.5, 45.5), (99999, 3600.0), (0.2, 1.0)])
def test_retry_after_from_retool_decides_the_pause_when_present(hint, pause):
    account = Account("a")
    value, _ = dispatcher([account])
    value.mark_rate_limited(account, retry_after=hint)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (pause, "retry_after")


@pytest.mark.parametrize("hint", [None, 0, -3, float("nan"), float("inf"), "soon"])
def test_an_unusable_retry_after_falls_back_to_the_cooldown(hint):
    account = Account("a")
    value, _ = dispatcher([account], account_rate_limit_cooldown=300)
    value.mark_rate_limited(account, retry_after=hint)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (300.0, "cooldown")


def test_the_scheduler_returns_the_account_when_the_pause_is_over(capsys):
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    before = journal.JOURNAL.last_seq
    value.mark_rate_limited(account)
    clock.now += 899
    value._tick()
    assert account.unavailable["reason"] == "rate_limited"
    assert not _events("account.available", before)
    clock.now += 2
    value._tick()
    assert account.unavailable is None
    returned = _events("account.available", before)
    assert len(returned) == 1 and returned[0]["data"]["account"] == value._tag(account)
    assert "снова в пуле" in capsys.readouterr().out


def test_the_real_scheduler_thread_releases_the_account_by_itself():
    account = Account("a")
    value = AccountDispatcher([account], {"session_refresh_interval": 0, "min_account_balance": 20}, {})
    before = journal.JOURNAL.last_seq
    value.start()
    try:
        value.mark_rate_limited(account, retry_after=1)
        deadline = time.monotonic() + 8
        while account.unavailable is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert account.unavailable is None
    finally:
        value.stop()
    assert len(_events("account.available", before)) == 1


def test_turns_that_fail_together_share_one_pause_and_one_journal_line():
    account = Account("a")
    value, _ = dispatcher([account])
    before = journal.JOURNAL.last_seq
    first = value.mark_rate_limited(account, request_id="req-1")
    second = value.mark_rate_limited(account, request_id="req-2", retry_after=30)
    assert first == second and account.unavailable["pause"] == 900.0
    events = _events("account.rate_limited", before)
    assert len(events) == 1
    assert events[0]["request_id"] == "req-1" and events[0]["data"]["account"] == value._tag(account)
    assert events[0]["data"]["until"] == pytest.approx(first, abs=0.01)


def test_a_failure_right_after_the_return_adds_five_minutes_up_to_twenty(capsys):
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    pauses = []
    for gap in (60, 100, 100, 100):
        value.mark_rate_limited(account)
        pauses.append((account.unavailable["pause"], account.unavailable["source"]))
        clock.now += account.unavailable["pause"]
        assert value.release_rate_limited()
        clock.now += gap
    assert pauses == [(900.0, "cooldown"), (1200.0, "escalated"), (1200.0, "escalated"), (1200.0, "escalated")]
    assert "повторный лимит подряд" in capsys.readouterr().out


@pytest.mark.parametrize("cooldown,ladder", [
    (600, [600, 900, 1200, 1200]),
    (900, [900, 1200, 1200, 1200]),
    (1200, [1200, 1200, 1200]),
    (1800, [1800, 1800, 1800]),  # a longer cooldown is its own ceiling: the ladder never shortens it
    (3600, [3600, 3600]),
])
def test_the_ladder_starts_at_the_cooldown_and_stops_at_the_larger_of_it_and_twenty_minutes(cooldown, ladder):
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0, account_rate_limit_cooldown=cooldown)
    seen = []
    for _ in ladder:
        value.mark_rate_limited(account)
        seen.append(account.unavailable["pause"])
        clock.now += account.unavailable["pause"]
        value.release_rate_limited()
        clock.now += 1
    assert seen == [float(step) for step in ladder]


def test_an_account_that_held_longer_than_it_was_parked_starts_over():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_rate_limited(account)
    clock.now += 900
    value.release_rate_limited()
    clock.now += 100
    value.mark_rate_limited(account)
    assert account.unavailable["pause"] == 1200.0
    clock.now += 1200
    value.release_rate_limited()
    clock.now += 1201  # held longer than the last pause: a new incident, not a streak
    value.mark_rate_limited(account)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (900.0, "cooldown")


def test_a_late_mark_after_the_deadline_counts_as_a_failure_right_after_the_return():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_rate_limited(account)
    clock.now += 905  # the scheduler has not ticked yet
    value.mark_rate_limited(account)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (1200.0, "escalated")


# -- a limit reported by a request that was sent before the account came back is old news -----------------------
def parked_and_returned(**cfg):
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0, **cfg)
    value.mark_rate_limited(account)
    clock.now += value.rate_limit_cooldown + 0.5
    assert value.release_rate_limited()
    return account, value, clock


def test_a_429_of_a_request_sent_before_the_return_changes_nothing():
    account, value, clock = parked_and_returned(account_rate_limit_cooldown=600)
    before = journal.JOURNAL.last_seq
    clock.now += 0.2  # the request was in flight all this time and only now reports its 429
    assert value.mark_rate_limited(account, sent_at=1000.0) is None
    assert account.unavailable is None
    assert not _events("account.rate_limited", before)
    # It is no streak either: the next real limit climbs one step from the first pause, not two.
    clock.now += 50
    value.mark_rate_limited(account, sent_at=clock.now - 1.0)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (900.0, "escalated")


def test_a_429_of_a_request_sent_after_the_return_is_a_new_limit():
    account, value, clock = parked_and_returned()
    clock.now += 0.2
    assert value.mark_rate_limited(account, sent_at=clock.now - 0.1) is not None
    assert (account.unavailable["pause"], account.unavailable["source"]) == (1200.0, "escalated")


def test_a_request_sent_at_the_very_moment_of_the_return_is_a_new_limit():
    account, value, clock = parked_and_returned()
    assert value.mark_rate_limited(account, sent_at=clock.now) is not None


def test_a_stale_429_does_not_extend_a_pause_that_is_still_running():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    until = value.mark_rate_limited(account)
    clock.now += 300
    assert value.mark_rate_limited(account, sent_at=1000.0) == until
    assert account.unavailable["pause"] == 900.0


def test_a_stale_429_after_the_deadline_but_before_the_scheduler_tick_is_ignored_too():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_rate_limited(account)
    clock.now += 905  # past the deadline, not released yet
    assert value.mark_rate_limited(account, sent_at=1000.0) is None
    assert account.unavailable["reason"] == "rate_limited" and account.unavailable["pause"] == 900.0


def test_a_limit_with_no_account_history_is_never_stale():
    account = Account("a")
    value, _ = dispatcher([account], session_refresh_interval=0)
    assert value.mark_rate_limited(account, sent_at=-5.0) is not None


@pytest.mark.parametrize("sent_at", [None, float("nan"), float("inf"), "later", True])
def test_without_a_usable_send_time_a_limit_counts_as_fresh(sent_at):
    account, value, clock = parked_and_returned()
    clock.now += 0.2
    assert value.mark_rate_limited(account, sent_at=sent_at) is not None
    assert account.unavailable["source"] == "escalated"


# -- Retry-After inside the ladder ---------------------------------------------------------------------------
def test_a_hint_is_honoured_as_it_is_and_never_escalated():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    for _ in range(3):
        value.mark_rate_limited(account, retry_after=60)
        assert (account.unavailable["pause"], account.unavailable["source"]) == (60.0, "retry_after")
        clock.now += 61
        value.release_rate_limited()
        clock.now += 1


def test_a_short_hint_does_not_make_the_next_unhinted_pause_short():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_rate_limited(account, retry_after=2)
    clock.now += 3
    value.release_rate_limited()
    clock.now += 1
    value.mark_rate_limited(account)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (900.0, "cooldown")


def test_a_hint_in_the_middle_of_a_streak_neither_advances_nor_resets_the_ladder():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_rate_limited(account)
    clock.now += 900
    value.release_rate_limited()
    clock.now += 10
    value.mark_rate_limited(account)
    assert account.unavailable["pause"] == 1200.0
    clock.now += 1200
    value.release_rate_limited()
    clock.now += 10
    value.mark_rate_limited(account, retry_after=30)
    assert account.unavailable["source"] == "retry_after"
    clock.now += 31
    value.release_rate_limited()
    clock.now += 10
    value.mark_rate_limited(account)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (1200.0, "escalated")



def test_a_parked_account_is_never_a_reserve_even_with_credits():
    account = Account("a")
    manager = FakeManager(5.0)
    value, clock = dispatcher([account], {account: manager}, session_refresh_interval=0)
    assert value.check_balance(account) is True
    assert SessionPool._reserve([account]) == [account]
    before = journal.JOURNAL.last_seq
    value.mark_rate_limited(account)
    assert account.unavailable["reason"] == "rate_limited" and SessionPool._reserve([account]) == []
    # The balance check keeps running while parked, without changing what routing sees.
    manager.available = 500
    assert value.check_balance(account) is False
    assert account.unavailable["reason"] == "rate_limited"
    manager.available = 4.0
    value.check_balance(account)
    clock.now += 901
    value.release_rate_limited()
    assert account.unavailable["reason"] == "exhausted"
    assert not _events("account.available", before)


def test_a_signed_out_account_stays_signed_out_after_its_pause():
    account = Account("a", FakeRefresher([(False, "auth")] * DEAD_AFTER))
    value, clock = dispatcher([account], session_refresh_interval=0)
    for _ in range(DEAD_AFTER):
        value.refresh(account)
    value.mark_rate_limited(account)
    assert account.unavailable["reason"] == "rate_limited"
    clock.now += 901
    value.release_rate_limited()
    assert account.unavailable["reason"] == "auth_dead"


def test_marking_an_unknown_account_does_nothing():
    value, _ = dispatcher([Account("a")])
    assert value.mark_rate_limited(Account("stranger")) is None


# -- the setting ---------------------------------------------------------------------
@pytest.fixture
def clean_env(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RETOOL_THREAD_ID",
                 "RELAY_SESSION_MODE", "RELAY_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "synthetic-key")


def _config_file(tmp_path, **extra):
    data = {"base": "https://a.invalid", "page_uuid": "p", "cookie": "accessToken=x; xsrfToken=y", "xsrf": "y",
            "ua": "t", "workspace_root": str(tmp_path), **extra}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_cooldown_defaults_to_fifteen_minutes_and_accepts_one_second_to_one_hour(tmp_path, clean_env):
    assert config.load(_config_file(tmp_path))["account_rate_limit_cooldown"] == 900
    for value in (1, 1800, 3600, "600"):
        loaded = config.load(_config_file(tmp_path, account_rate_limit_cooldown=value))
        assert loaded["account_rate_limit_cooldown"] == int(value)


@pytest.mark.parametrize("value", [0, -1, 3601, "abc", None])
def test_cooldown_outside_one_second_to_one_hour_is_refused(tmp_path, clean_env, value):
    with pytest.raises(apierr.ApiError) as caught:
        config.load(_config_file(tmp_path, account_rate_limit_cooldown=value))
    assert "account_rate_limit_cooldown" in caught.value.message


def test_the_dispatcher_uses_the_loaded_cooldown(tmp_path, clean_env):
    loaded = config.load(_config_file(tmp_path, account_rate_limit_cooldown=1200))
    account = Account("a")
    value = AccountDispatcher([account], loaded, {})
    value.mark_rate_limited(account)
    assert account.unavailable["pause"] == 1200.0


def test_the_admin_schema_lists_the_cooldown_as_a_routing_setting(admin_relay):
    field = admin._schema()["account_rate_limit_cooldown"]
    assert (field["group"], field["unit"], field["min"], field["max"]) == ("routing", "с", 1, 3600)
    status, body, _ = admin_relay.patch({"account_rate_limit_cooldown": 1200})
    assert status == 200 and body["applied"] == [] and body["pending_restart"] == ["account_rate_limit_cooldown"]
    assert admin_relay.patch({"account_rate_limit_cooldown": 4000})[0] == 400


# -- the turn moves to another account -----------------------------------------------
def serve_ok(_request):
    return response(text("OK"), *END)


def hourly(request_):
    raise urllib.error.HTTPError(request_.full_url, 429, "Too Many Requests", Message(),
                                 io.BytesIO(HOURLY_BODY.encode()))


def short_429(request_):
    raise urllib.error.HTTPError(request_.full_url, 429, "Too Many Requests", Message(),
                                 io.BytesIO(SHORT_BODY.encode()))


def tag(account_id):
    return journal.tag(account_id)


@pytest.fixture
def retool(tmp_path, monkeypatch):
    """Two accounts behind one pool, a real dispatcher and the real direct transport on a scripted Retool."""
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    settings = dict(transport="direct", project_context=False, cookie_values=[], cache_keepwarm_seconds=0,
                    upstream_error_dir=str(tmp_path))
    first, pool, runner = setup(tmp_path, base="https://acc-a.invalid", account_id="acc-a", **settings)
    second = Backend(tmp_path, base="https://acc-b.invalid", account_id="acc-b", **settings)
    pool.backends.append(second)
    for backend in pool.backends:
        backend.headers = lambda: {}
    clock = Clock()
    manager = AccountDispatcher(list(pool.backends), runner.cfg, {}, clock=clock, rng=random.Random(1))
    runner.accounts = manager
    posts, script, paid = [], {}, []
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: paid.append((key, cost)))

    def opener(request_, **_kwargs):
        account = request_.full_url.split("/")[2].split(".")[0]
        posts.append(account)
        return script.get(account, serve_ok)(request_)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return SimpleNamespace(pool=pool, runner=runner, dispatcher=manager, clock=clock, a=first, b=second,
                           posts=posts, script=script, paid=paid)


def turn(conversation="conv-1"):
    parsed = request(FIRST, key=conversation)
    parsed.request_id = "req-" + conversation
    return parsed


def test_a_turn_that_hits_the_hourly_limit_moves_to_the_next_account_unseen(retool):
    retool.script["acc-a"] = hourly
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    assert retool.posts == ["acc-a", "acc-b"]
    assert {"type": "text", "delta": "OK"} in events and events[-1] == {"type": "end", "reason": "stop"}
    assert not any(event["type"] == "error" for event in events)
    assert retool.a.unavailable["reason"] == "rate_limited" and retool.b.unavailable is None
    assert retool.pool._conversations["conv-1"].backend is retool.b
    # The rejected attempt cost nothing and is not reported as an unknown charge.
    assert [cost for _, cost in retool.paid] == [0.1]
    assert not any(event["type"] == "usage" and not event.get("usage") for event in events)
    parked = _events("account.rate_limited", before)
    assert len(parked) == 1 and parked[0]["request_id"] == "req-conv-1"
    assert parked[0]["data"]["account"] == tag("acc-a") and parked[0]["data"]["until"] > time.time()
    moved = _events("account.failover", before)
    assert len(moved) == 1 and moved[0]["request_id"] == "req-conv-1"
    assert moved[0]["data"]["account"] == tag("acc-a")
    assert moved[0]["data"]["error"] == "rate_limit_error" and moved[0]["data"]["reason"] == "rate_limited"
    assert not retool.pool._active


def test_the_retry_after_header_sets_the_pause(retool):
    def hinted(request_):
        raise urllib.error.HTTPError(request_.full_url, 429, "Too Many Requests", _headers(retry_after="120"),
                                     io.BytesIO(HOURLY_BODY.encode()))

    retool.script["acc-a"] = hinted
    asyncio.run(run(retool.runner, turn()))
    assert (retool.a.unavailable["pause"], retool.a.unavailable["source"]) == (120.0, "retry_after")


def test_a_bound_conversation_moves_when_its_account_is_parked(retool):
    async def scenario():
        await run(retool.runner, turn())
        assert retool.pool._conversations["conv-1"].backend is retool.a
        retool.posts.clear()
        retool.dispatcher.mark_rate_limited(retool.a)
        before = journal.JOURNAL.last_seq
        return before, await run(retool.runner, turn())

    before, events = asyncio.run(scenario())
    assert retool.posts == ["acc-b"]  # the parked account is not even tried
    assert {"type": "text", "delta": "OK"} in events
    assert retool.pool._conversations["conv-1"].backend is retool.b
    migrated = _events("account.migrate", before)
    assert len(migrated) == 1 and migrated[0]["data"]["reason"] == "rate_limited"


def test_the_account_returns_after_the_pause_and_new_conversations_use_it_again(retool):
    async def scenario():
        await run(retool.runner, turn("conv-1"))
        retool.dispatcher.mark_rate_limited(retool.a)
        await run(retool.runner, turn("conv-1"))
        retool.clock.now += 901
        assert retool.dispatcher.release_rate_limited()
        await run(retool.runner, turn("conv-2"))
        await run(retool.runner, turn("conv-1"))

    asyncio.run(scenario())
    assert retool.a.unavailable is None
    # The old conversation does not bounce back (its prefix cache lives on the new account).
    assert retool.pool._conversations["conv-1"].backend is retool.b
    assert retool.pool._conversations["conv-2"].backend is retool.a


def test_the_first_real_request_after_the_pause_parks_the_account_again_for_longer(retool):
    retool.script["acc-a"] = hourly

    async def scenario():
        await run(retool.runner, turn("conv-1"))
        retool.clock.now += 901
        retool.dispatcher.release_rate_limited()
        return await run(retool.runner, turn("conv-2"))

    events = asyncio.run(scenario())
    assert retool.posts == ["acc-a", "acc-b", "acc-a", "acc-b"]
    assert {"type": "text", "delta": "OK"} in events and not any(event["type"] == "error" for event in events)
    assert (retool.a.unavailable["pause"], retool.a.unavailable["source"]) == (1200.0, "escalated")


def test_a_limit_reported_by_a_request_that_left_before_the_pause_is_not_a_second_limit(retool):
    def late(request_):
        # While this request is in flight another one parks the account and the pause runs out.
        retool.dispatcher.mark_rate_limited(retool.a)
        retool.clock.now += 901
        retool.dispatcher.release_rate_limited()
        return hourly(request_)

    retool.script["acc-a"] = late
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    # The request still moves to the other account and is answered ...
    assert retool.posts == ["acc-a", "acc-b"]
    assert {"type": "text", "delta": "OK"} in events and not any(event["type"] == "error" for event in events)
    # ... but the old 429 neither parks the returned account again nor lengthens anything.
    assert retool.a.unavailable is None
    assert len(_events("account.rate_limited", before)) == 1
    assert len(_events("account.failover", before)) == 1


def test_a_limit_from_a_request_that_left_after_the_return_parks_the_account_again(retool):
    retool.script["acc-a"] = hourly

    async def scenario():
        await run(retool.runner, turn("conv-1"))
        retool.clock.now += 901
        retool.dispatcher.release_rate_limited()
        await run(retool.runner, turn("conv-2"))

    asyncio.run(scenario())
    assert retool.a.unavailable["reason"] == "rate_limited"


def test_when_every_account_is_limited_the_client_gets_a_429_with_retry_after(retool):
    retool.script["acc-a"] = retool.script["acc-b"] = hourly

    async def scenario():
        first = await run(retool.runner, turn("conv-1"))
        posts = len(retool.posts)
        second = await run(retool.runner, turn("conv-2"))
        return first, posts, second

    first, posts, second = asyncio.run(scenario())
    assert posts == 2 and retool.posts == ["acc-a", "acc-b"]  # the second turn never reached Retool
    for events in (first, second):
        error = events[-1]["error"]
        assert (error.status, error.code) == (429, "rate_limit_error")
        assert "все аккаунты упёрлись в часовой лимит Retool" in error.message
        assert 890 <= error.retry_after <= 900 and "15 мин" in error.message
        assert not apierr.is_account_quota(error)
    assert {retool.a.unavailable["reason"], retool.b.unavailable["reason"]} == {"rate_limited"}


def test_a_limit_mixed_with_other_unusable_accounts_still_answers_429(retool):
    retool.dispatcher.mark_rate_limited(retool.a, retry_after=120)
    retool.b.unavailable = {"reason": "exhausted", "available": 0.0, "threshold": 20.0}
    error = SessionPool._unusable_error(retool.pool.backends)
    assert (error.status, error.code) == (429, "rate_limit_error")
    assert "1 из 2" in error.message and 100 <= error.retry_after <= 120


def test_an_ordinary_429_is_returned_as_before_without_parking_the_account(retool):
    retool.script["acc-a"] = short_429
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    error = events[-1]["error"]
    assert retool.posts == ["acc-a"]
    assert (error.status, error.code, error.message) == (429, "rate_limit_error", "Slow down, try again")
    assert retool.a.unavailable is None
    assert not _events("account.rate_limited", before) and not _events("account.failover", before)


def test_an_hourly_error_as_the_very_first_stream_event_still_moves_the_turn(retool):
    retool.script["acc-a"] = lambda _request: response(
        {"event": "error", "statusCode": 429, "error": {"message": MESSAGE}})
    events = asyncio.run(run(retool.runner, turn()))
    assert retool.posts == ["acc-a", "acc-b"] and {"type": "text", "delta": "OK"} in events
    assert not any(event["type"] == "error" for event in events)
    assert retool.a.unavailable["reason"] == "rate_limited"


def test_an_hourly_error_in_the_middle_of_a_stream_parks_the_account_for_the_next_request(retool):
    retool.script["acc-a"] = lambda _request: response(
        text("par"), {"event": "error", "statusCode": 429, "error": {"message": MESSAGE}})

    async def scenario():
        first = await run(retool.runner, turn())
        second = await run(retool.runner, turn())
        return first, second

    first, second = asyncio.run(scenario())
    # Part of the answer is already with the client: no silent retry, but the account is parked.
    assert {"type": "text", "delta": "par"} in first
    assert first[-1]["type"] == "error" and first[-1]["error"].status == 429
    assert retool.a.unavailable["reason"] == "rate_limited"
    assert retool.posts == ["acc-a", "acc-b"]
    assert {"type": "text", "delta": "OK"} in second and not any(event["type"] == "error" for event in second)
    assert retool.pool._conversations["conv-1"].backend is retool.b


def test_without_attempts_left_the_account_is_parked_but_the_turn_is_not_retried(retool, monkeypatch):
    monkeypatch.setattr(upstream_attempts, "MAX_ATTEMPTS", 1)
    retool.script["acc-a"] = hourly
    events = asyncio.run(run(retool.runner, turn()))
    error = events[-1]["error"]
    assert retool.posts == ["acc-a"] and error.status == 429 and error.message == MESSAGE
    assert retool.a.unavailable["reason"] == "rate_limited" and retool.b.unavailable is None


def test_a_dispatcher_without_marking_still_moves_the_turn(retool):
    retool.runner.accounts = SimpleNamespace(check_balance_now=lambda backend: False)
    retool.script["acc-a"] = hourly
    events = asyncio.run(run(retool.runner, turn()))
    assert retool.posts == ["acc-a", "acc-b"] and {"type": "text", "delta": "OK"} in events
    assert retool.a.unavailable is None


def test_a_lone_parked_account_answers_429_and_keeps_its_conversation(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)

    async def scenario():
        lease = await pool.acquire("chat")
        first = lease.session
        await pool.release(lease)
        pool.backends[0].unavailable = {"reason": "rate_limited", "until_at": time.time() + 600, "pause": 900.0}
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire("chat")
        assert pool._conversations["chat"] is first
        assert not pool._active and pool._slots._value == 8
        return caught.value

    error = asyncio.run(scenario())
    assert (error.status, error.code) == (429, "rate_limit_error") and 590 <= error.retry_after <= 600


def test_shared_mode_refuses_a_parked_account_without_calling_retool(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False, session_mode="shared")
    pool.backends[0].unavailable = {"reason": "rate_limited", "until_at": time.time() + 120, "pause": 900.0}

    async def scenario():
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire("chat")
        assert pool._slots._value == 8
        return caught.value

    error = asyncio.run(scenario())
    assert error.status == 429 and 110 <= error.retry_after <= 120


# -- what the client and the operator see ---------------------------------------------
RELAY_KEY = "rate-limit-relay-key-123"
CHAT = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def served(monkeypatch, tmp_path):
    """The real relay (two accounts, real TurnRunner and transport) over HTTP, with a scripted Retool."""
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())

    def account(name):
        return {"account_id": name, "page_uuid": "page-" + name, "thread_id": "thread-" + name,
                "cookie": "accessToken=cookie-%s-123456; xsrfToken=q" % name, "xsrf": "q",
                "provider_resource_name": "resource-" + name}

    cfg = dict(config.DEFAULTS)
    cfg.update({"base": "https://example.invalid", "page_uuid": "page-acc-a", "thread_id": "thread-a",
                "ua": "test-agent", "cookie": "accessToken=cookie-acc-a-123456; xsrfToken=q", "xsrf": "q",
                "relay_api_key": RELAY_KEY, "workspace_root": str(tmp_path), "command_log": str(tmp_path / "c.log"),
                "host": "127.0.0.1", "port": 0, "cookie_values": [], "client_metadata_home": str(tmp_path),
                "cache_keepwarm_seconds": 0, "upstream_error_dir": str(tmp_path),
                "accounts": [account("acc-a"), account("acc-b")]})
    runtime = proxy.Runtime(cfg)
    managers = [BalanceManager(100.0), BalanceManager(80.0)]
    for backend, manager in zip(runtime.accounts.backends, managers):
        runtime.accounts.managers[backend] = manager
        backend.credits = manager
    runtime.credits = managers[0]
    script, posts = {}, []

    def opener(request_, **_kwargs):
        who = "acc-a" if "cookie-acc-a" in (request_.get_header("Cookie") or "") else "acc-b"
        posts.append(who)
        return script.get(who, serve_ok)(request_)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(path, payload=None, *, key=RELAY_KEY):
        # http.client: urllib's urlopen is the scripted Retool in these tests.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = "Bearer " + key
        connection.request("POST" if payload is not None else "GET", path,
                           json.dumps(payload).encode() if payload is not None else None, headers)
        response = connection.getresponse()
        raw = response.read().decode()
        connection.close()
        is_json = "json" in response.headers.get("content-type", "")
        return response.status, (json.loads(raw or "{}") if is_json else raw), response.headers

    yield SimpleNamespace(runtime=runtime, script=script, posts=posts, call=call,
                          dispatcher=runtime.accounts, backends=runtime.accounts.backends)
    server.shutdown()
    try:
        server.server_close()
    except OSError:
        pass
    runtime.close()


def test_the_client_never_sees_the_hourly_limit_while_another_account_is_free(served):
    served.script["acc-a"] = hourly
    status, body, _ = served.call("/v1/chat/completions", CHAT)
    assert status == 200 and body["choices"][0]["message"]["content"] == "OK"
    assert served.posts == ["acc-a", "acc-b"]
    assert served.backends[0].unavailable["reason"] == "rate_limited"


def test_the_parked_account_and_its_return_time_are_visible_everywhere(served):
    served.script["acc-a"] = hourly
    assert served.call("/v1/chat/completions", CHAT)[0] == 200
    until = served.backends[0].unavailable["until_at"]
    assert time.time() < until <= time.time() + 900

    status, health, _ = served.call("/health", key=None)
    assert status == 200
    assert health["accounts"] == {"total": 2, "routable": 1, "reserve": 0, "rate_limited_until": until,
                                  "states": {"rate_limited": 1, "active": 1}}

    status, balance, _ = served.call("/v1/balance")
    parked, free = balance["accounts"]
    assert (parked["state"], parked["rate_limited_until"]) == ("rate_limited", until)
    assert free["state"] == "active" and "rate_limited_until" not in free

    status, overview, _ = served.call("/admin/api/overview")
    parked, free = overview["accounts"]
    assert (parked["state"], parked["routable"], parked["rate_limited_until"]) == ("rate_limited", False, until)
    assert free["state"] == "active" and "rate_limited_until" not in free
    assert overview["accounts_summary"]["states"] == {"rate_limited": 1, "active": 1}

    status, pool, _ = served.call("/admin/api/accounts")
    parked, free = pool["accounts"]
    assert (parked["state"], parked["rate_limited_until"]) == ("rate_limited", until)
    assert free["state"] == "active" and "rate_limited_until" not in free
    assert pool["summary"]["states"] == {"rate_limited": 1, "active": 1}


def test_when_every_account_is_parked_the_http_answer_is_429_with_retry_after_not_503(served):
    served.script["acc-a"] = served.script["acc-b"] = hourly
    status, body, headers = served.call("/v1/chat/completions", CHAT)
    assert status == 429 and served.posts == ["acc-a", "acc-b"]
    assert 1 <= int(headers["retry-after"]) <= 900
    assert body["error"]["type"] == "rate_limit_error"
    assert "все аккаунты упёрлись в часовой лимит Retool" in body["error"]["message"]
    # Parked accounts are not tried again: the next request is answered locally.
    status, body, headers = served.call("/v1/messages", {**CHAT, "max_tokens": 64})
    assert status == 429 and served.posts == ["acc-a", "acc-b"]
    assert body["error"]["type"] == "rate_limit_error" and int(headers["retry-after"]) >= 1

    status, health, _ = served.call("/health", key=None)
    assert status == 503 and health["accounts"]["states"] == {"rate_limited": 2}
    assert health["accounts"]["routable"] == 0 and health["upstream"] == "degraded"


def test_a_streaming_client_gets_the_real_429_while_every_account_is_parked(served):
    served.dispatcher.mark_rate_limited(served.backends[0])
    served.dispatcher.mark_rate_limited(served.backends[1])
    status, body, headers = served.call("/v1/chat/completions", {**CHAT, "stream": True})
    assert status == 429 and 1 <= int(headers["retry-after"]) <= 900
    assert body["error"]["type"] == "rate_limit_error"
    assert "все аккаунты упёрлись в часовой лимит Retool" in body["error"]["message"]
    assert served.posts == []


def test_an_account_parked_after_the_check_still_ends_in_an_error_event(served, monkeypatch):
    # The race the pre-flight check cannot close: acquire() fails once the stream has begun.
    monkeypatch.setattr(served.runtime.pool, "unavailable_error", lambda *args, **kwargs: None)
    served.dispatcher.mark_rate_limited(served.backends[0])
    served.dispatcher.mark_rate_limited(served.backends[1])
    status, body, _ = served.call("/v1/chat/completions", {**CHAT, "stream": True})
    assert status == 200 and "rate_limit_error" in body and "все аккаунты упёрлись в часовой лимит Retool" in body
    assert served.posts == []


def test_an_ordinary_429_reaches_the_client_and_parks_nothing(served):
    served.script["acc-a"] = short_429
    status, body, headers = served.call("/v1/chat/completions", CHAT)
    assert status == 429 and body["error"]["message"] == "Slow down, try again"
    assert served.posts == ["acc-a"]
    assert all(backend.unavailable is None for backend in served.backends)
    status, health, _ = served.call("/health", key=None)
    assert "rate_limited_until" not in health["accounts"] and health["accounts"]["states"] == {"active": 2}
