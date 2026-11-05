"""Retool's hourly managed-key limit is an account quota: it parks the account and moves the turn."""
import asyncio
import io
import json
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
from accounts import DEAD_AFTER, AccountDispatcher
from session import SessionPool
from test_account_dispatcher import Account, FakeManager, FakeRefresher, dispatcher
from test_admin import admin_relay  # noqa: F401 - fixture

MESSAGE = ("You have exceeded your hourly limit of managed API key usage. Please go to the Retool AI resource "
           "in the Resources page to configure your own API keys.")
HOURLY_BODY = json.dumps({"statusCode": 429, "error": "Too Many Requests", "message": MESSAGE})
SHORT_BODY = json.dumps({"statusCode": 429, "error": "Too Many Requests", "message": "Slow down, try again"})


def _headers(**values):
    message = Message()
    for name, value in values.items():
        message[name.replace("_", "-")] = value
    return message


# -- 1: recognition ----------------------------------------------------------------
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


# -- 2: parking the account ----------------------------------------------------------
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


def test_a_failure_right_after_the_return_doubles_the_pause_up_to_an_hour(capsys):
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    pauses = []
    for gap in (60, 100, 100, 100):
        value.mark_rate_limited(account)
        pauses.append((account.unavailable["pause"], account.unavailable["source"]))
        clock.now += account.unavailable["pause"]
        assert value.release_rate_limited()
        clock.now += gap
    assert pauses == [(900.0, "cooldown"), (1800.0, "escalated"), (3600.0, "escalated"), (3600.0, "escalated")]
    assert "повторный лимит подряд" in capsys.readouterr().out


def test_an_account_that_held_longer_than_it_was_parked_starts_over():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_rate_limited(account)
    clock.now += 900
    value.release_rate_limited()
    clock.now += 100
    value.mark_rate_limited(account)
    assert account.unavailable["pause"] == 1800.0
    clock.now += 1800
    value.release_rate_limited()
    clock.now += 1801  # held longer than the last pause: a new incident, not a streak
    value.mark_rate_limited(account)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (900.0, "cooldown")


def test_a_late_mark_after_the_deadline_counts_as_a_failure_right_after_the_return():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_rate_limited(account)
    clock.now += 905  # the scheduler has not ticked yet
    value.mark_rate_limited(account)
    assert (account.unavailable["pause"], account.unavailable["source"]) == (1800.0, "escalated")


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


# -- 8: the setting ------------------------------------------------------------------
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
