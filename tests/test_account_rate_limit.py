"""Retool's hourly managed-key limit is an account quota: it parks the account and moves the turn."""
import asyncio
import io
import json
import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest

import apierr
import direct

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
