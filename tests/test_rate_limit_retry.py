"""Retool's "models are rate limited right now" (429) is asked again on the same account, within the attempt budget.

The refusal comes before anything is generated, in an error event inside a 200 (the GLM case: 5 accounts within two minutes
of one test run) or as an HTTP 429. Moving the conversation to another account would start its history and cache from nothing,
so the relay waits a few seconds and sends the same body again; what it cannot do is make Retool's limit shorter."""
import asyncio
import io
import json
import urllib.error

import pytest

import adapters
import apierr
import config
import direct
import journal
import validate
from upstream_attempts import AttemptBudget
from test_native_context_reuse import FIRST, PARSE_CFG, run, setup
from test_review_fixes_pr19 import END, response, stream_direct, text

LIMIT = "Retool models are rate limited right now. Please try again shortly."
REFUSED = [{"event": "error", "error": LIMIT, "statusCode": 429}]
ANSWER = [text("ok"), *END]


@pytest.fixture(autouse=True)
def waits(monkeypatch):
    """No real sleeping: the pauses the relay asks for are recorded instead."""
    found = []

    async def pause(seconds, is_cancelled):
        found.append(seconds)
        return not is_cancelled()

    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    monkeypatch.setattr(direct, "_DUMP_RECENT", {})
    monkeypatch.setattr(direct, "_pause", pause)
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)
    return found


def http_429():
    body = io.BytesIO(json.dumps({"message": LIMIT, "type": "rate_limit_error"}).encode())
    return urllib.error.HTTPError("https://fixture.invalid/", 429, "Too Many Requests", {}, body)


def go(tmp_path, monkeypatch, script, **cfg):
    calls, stream = stream_direct(tmp_path, monkeypatch, script, **cfg)
    try:
        return calls, asyncio.run(stream()), None
    except apierr.ApiError as error:
        return calls, None, error


def within(value, base):
    return base * 0.75 <= value <= base * 1.25


def test_a_429_event_is_asked_again_and_the_answer_is_whole(tmp_path, monkeypatch, waits):
    calls, events, error = go(tmp_path, monkeypatch, [REFUSED, REFUSED, ANSWER])
    assert error is None and len(calls) == 3 and len(set(calls)) == 1  # the same body, every time
    assert [e["delta"] for e in events if e["type"] == "text"] == ["ok"] and events[-1] == {"type": "end", "reason": "stop"}
    assert len(waits) == 2 and within(waits[0], 1.5) and within(waits[1], 3.0)


def test_an_http_429_is_asked_again_too(tmp_path, monkeypatch, waits):
    calls, events, error = go(tmp_path, monkeypatch, [http_429(), ANSWER])
    assert error is None and len(calls) == 2 and len(waits) == 1
    assert any(e["type"] == "text" for e in events)


def test_when_the_limit_stays_the_client_gets_the_429_after_three_more_tries(tmp_path, monkeypatch, waits):
    calls, events, error = go(tmp_path, monkeypatch, [REFUSED])
    assert len(calls) == 4 and len(waits) == 3 and within(waits[2], 6.0)
    assert (error.status, error.code) == (429, "rate_limit_error") and error.message == LIMIT
    assert error.upstream_attempts == 4 and error.retry_after == 1.0 and not apierr.is_account_quota(error)


def test_zero_retries_pass_the_429_on_at_once(tmp_path, monkeypatch, waits):
    calls, _, error = go(tmp_path, monkeypatch, [REFUSED], upstream_rate_limit_retries=0)
    assert len(calls) == 1 and waits == [] and error.status == 429


def test_the_five_post_budget_of_a_step_is_never_exceeded(tmp_path, monkeypatch, waits):
    calls, _, error = go(tmp_path, monkeypatch, [REFUSED], upstream_rate_limit_retries=9)
    assert len(calls) == 5 == error.upstream_attempts and len(waits) == 4
    assert within(waits[3], 6.0)  # the last pause stays at the longest one


def test_the_hourly_quota_of_an_account_is_not_this_limit_and_is_not_asked_again_here(tmp_path, monkeypatch, waits):
    quota = [{"event": "error", "error": "You have reached the hourly limit of managed api key usage", "statusCode": 429}]
    calls, _, error = go(tmp_path, monkeypatch, [quota])
    assert len(calls) == 1 and waits == [] and apierr.is_account_quota(error)


def test_an_answer_that_has_begun_is_never_asked_again(tmp_path, monkeypatch, waits):
    # the text proves the attempt usable and is handed on, so a second answer must not follow it
    calls, events, error = go(tmp_path, monkeypatch, [[text("partial "), *REFUSED], ANSWER])
    assert len(calls) == 1 and waits == [] and error.status == 429


@pytest.mark.parametrize("status", [400, 500, 503])
def test_other_failures_are_not_asked_again(tmp_path, monkeypatch, waits, status):
    failure = [{"event": "error", "error": "something else", "statusCode": status}]
    calls, _, error = go(tmp_path, monkeypatch, [failure])
    assert len(calls) == 1 and waits == [] and error.status != 429


def test_the_wait_ends_when_the_client_leaves(tmp_path, monkeypatch, waits):
    async def cancelled(seconds, is_cancelled):
        waits.append(seconds)
        return False

    monkeypatch.setattr(direct, "_pause", cancelled)
    calls, events, error = go(tmp_path, monkeypatch, [REFUSED, ANSWER])
    assert len(calls) == 1 and error is None and events[-1] == {"type": "end", "reason": "cancelled"}


def test_the_pause_itself_waits_and_stops_on_cancel(monkeypatch):
    monkeypatch.undo()  # the real function, not the recording one
    assert asyncio.run(direct._pause(0.05, lambda: False)) is True
    assert asyncio.run(direct._pause(5.0, lambda: True)) is False


# -- the request as a whole ----------------------------------------------------------------------------------------------

def glm_turn(tmp_path, monkeypatch, scripts, **cfg):
    backend, pool, runner = setup(tmp_path, transport="direct", cache_keepwarm_seconds=0, base="https://fixture.invalid",
                                  cookie_values=[], project_context=False, retool_fast_provider_resource_name="__managed__",
                                  **cfg)
    backend.headers = lambda: {}
    sent = []

    def opener(req, **kwargs):
        sent.append(json.loads(req.data))
        item = scripts[min(len(sent), len(scripts)) - 1]
        if isinstance(item, Exception):
            raise item
        return response(*item)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    parsed = validate.parse_chat({"model": "zai-org/GLM-5.3", "messages": [{"role": "user", "content": "hi"}]}, PARSE_CFG)
    parsed.conversation_key = "header:glm"
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(runner, parsed))
    return events, sent, [e for e in journal.JOURNAL.query(since=before)], parsed


def test_a_glm_reply_after_two_refusals_is_whole_free_of_charge_and_stays_on_its_account(tmp_path, monkeypatch, waits):
    events, sent, journaled, parsed = glm_turn(tmp_path, monkeypatch, [REFUSED, http_429(), ANSWER])
    assert len(sent) == 3 and not any(e["type"] == "error" for e in events) and len(waits) == 2
    adapter = adapters.for_dialect("chat", parsed)
    adapter.begin()
    for event in events:
        if event["type"] not in ("end",):
            adapter.feed(event)
    final = adapter.end("stop")
    assert final["choices"][0]["message"]["content"] == "ok" and final["choices"][0]["finish_reason"] == "stop"
    # the two refusals cost nothing and say nothing about the price: it is the answer's, known
    assert final["usage"]["credits_complete"] is True and final["usage"]["credits_used"] == 0.1
    names = [e["kind"] for e in journaled]
    assert names.count("upstream.rate_limited") == 2 and "account.failover" not in names
    waited = [e for e in journaled if e["kind"] == "upstream.rate_limited"]
    assert [e["data"]["retry"] for e in waited] == [1, 2] and all(e["level"] == "warn" for e in waited)


def test_a_limit_that_stays_ends_the_request_as_a_429_without_moving_the_conversation(tmp_path, monkeypatch, waits):
    events, sent, journaled, _ = glm_turn(tmp_path, monkeypatch, [REFUSED])
    error = events[-1]["error"]
    assert (error.status, error.code, error.upstream_attempts) == (429, "rate_limit_error", 4) and len(sent) == 4
    assert "account.failover" not in [e["kind"] for e in journaled]


# -- the setting ---------------------------------------------------------------------------------------------------------

def load(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(tmp_path), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


def test_the_default_is_three_and_zero_to_four_are_accepted(tmp_path, monkeypatch):
    assert config.DEFAULTS["upstream_rate_limit_retries"] == 3
    assert load(tmp_path, monkeypatch)["upstream_rate_limit_retries"] == 3
    for value in (0, 1, 4):
        assert load(tmp_path, monkeypatch, upstream_rate_limit_retries=value)["upstream_rate_limit_retries"] == value


@pytest.mark.parametrize("value", [5, -1, "x", None])
def test_a_value_outside_the_budget_stops_the_start(tmp_path, monkeypatch, value):
    with pytest.raises(apierr.ApiError, match="upstream_rate_limit_retries"):
        load(tmp_path, monkeypatch, upstream_rate_limit_retries=value)


def test_the_example_config_has_it():
    from pathlib import Path
    example = json.loads((Path(config.HERE) / "config.example.json").read_text(encoding="utf-8"))
    assert example["upstream_rate_limit_retries"] == 3 and "429" in example["upstream_rate_limit_retries_note"]
