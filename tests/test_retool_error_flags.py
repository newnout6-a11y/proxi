"""Retool's isRetryable flag decides whether an error event is asked again, and a model that answers in place of the one asked
is journaled (R1a R3, R4; Retool's StreamHandler and streamWithRetry in its public frontend 4.99.0).

Every error event of Retool's stream carries isRetryable. Retool's own frontend asks the same request again only when it is
true (up to 3 times, 250 ms * 2^n apart) and never when it is false (it falls back to another model instead). The relay read
neither: a retryable error before the model gave anything went to the client as a 502 that the client sent again in full,
and a final one went out as a 502 that SDK clients retry up to ten times for nothing. The metadata event names the model
that answered; nothing compared it with the one asked for."""
import asyncio
from types import SimpleNamespace

import pytest

import apierr
import direct
import journal
from test_review_fixes_pr19 import END, response, text
from upstream_attempts import AttemptBudget

EOS = {"event": "end_of_stream"}
OVERLOADED = {"event": "error", "error": "Overloaded", "isRetryable": True, "isUsingRetoolManagedKey": True}
FINAL = {"event": "error", "error": "Something the provider will refuse again", "isRetryable": False}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)
    monkeypatch.setattr(direct, "RETRYABLE_WAITS", (0.0, 0.0, 0.0), raising=False)


def run(tmp_path, monkeypatch, script, model="claude-opus-5-5"):
    """(events, error, posts) of _stream_body over one scripted answer per POST."""
    cfg = {"base": "https://fixture.invalid", "upstream_error_dir": str(tmp_path / "diag")}
    posts = []

    def opener(_request, **_kw):
        posts.append(1)
        return response(*script[min(len(posts), len(script)) - 1])

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    session = SimpleNamespace(backend=SimpleNamespace(cfg=cfg, headers=lambda: {}), _cancelled=False)
    events = []

    async def go():
        try:
            async for event in direct._stream_body(cfg, session, {"model": model}):
                events.append(event)
        except apierr.ApiError as error:
            return error
        return None

    error = asyncio.run(go())
    return events, error, len(posts)


def test_a_retryable_error_before_anything_is_asked_again_on_the_same_account(tmp_path, monkeypatch):
    events, error, posts = run(tmp_path, monkeypatch, [[OVERLOADED, EOS], [text("ok"), *END]])
    assert error is None and posts == 2
    assert [event["delta"] for event in events if event["type"] == "text"] == ["ok"]
    assert events[-1] == {"type": "end", "reason": "stop"}


def test_a_retryable_error_that_keeps_coming_goes_out_after_three_retries(tmp_path, monkeypatch):
    events, error, posts = run(tmp_path, monkeypatch, [[OVERLOADED, EOS]] * 5)
    assert posts == 4, "the first try and three more, as Retool's own frontend"
    assert error is not None and error.message == "Overloaded" and error.retryable is True


def test_a_retryable_error_after_the_answer_began_is_not_asked_again(tmp_path, monkeypatch):
    events, error, posts = run(tmp_path, monkeypatch, [[text("par"), OVERLOADED, EOS], [text("ok"), *END]])
    assert posts == 1 and error is not None
    assert [event["delta"] for event in events if event["type"] == "text"] == ["par"]


def test_a_final_error_is_not_asked_again_and_tells_sdk_clients_not_to_resend_it(tmp_path, monkeypatch):
    _, error, posts = run(tmp_path, monkeypatch, [[FINAL, EOS], [text("ok"), *END]])
    assert posts == 1 and error.retryable is False
    assert error.status == 502 and error.headers.get("x-should-retry") == "false"


def test_an_error_event_without_the_flag_keeps_its_old_handling(tmp_path, monkeypatch):
    _, error, posts = run(tmp_path, monkeypatch, [[{"event": "error", "error": "boom"}, EOS], [text("ok"), *END]])
    assert posts == 1 and error.status == 502 and "x-should-retry" not in error.headers
    assert getattr(error, "retryable", None) is None


def test_the_hourly_limit_is_never_asked_again_on_its_account_whatever_the_flag_says(tmp_path, monkeypatch):
    hourly = {"event": "error", "statusCode": 429, "isRetryable": True,
              "error": "You've reached your hourly limit for Copilot. Please try again in an hour."}
    _, error, posts = run(tmp_path, monkeypatch, [[hourly, EOS], [text("ok"), *END]])
    assert posts == 1 and apierr.is_account_quota(error), "the orchestrator parks the account and moves the turn"


def metadata(model):
    return {"event": "metadata", "model": model, "usage": {"inputTokens": 4, "outputTokens": 1, "creditsUsed": 0.1}}


@pytest.mark.parametrize("served, substituted", [("claude-opus-5-5", False), ("gpt-5-nano", True)])
def test_a_model_that_answers_in_place_of_the_one_asked_is_journaled(tmp_path, monkeypatch, served, substituted):
    seq = journal.JOURNAL.last_seq
    events, error, _ = run(tmp_path, monkeypatch, [[text("ok"), metadata(served), EOS]])
    assert error is None and events[-1] == {"type": "end", "reason": "stop"}
    found = [event["data"] for event in journal.JOURNAL.query(since=seq, limit=1000)
             if event["kind"] == "upstream.model_substituted"]
    assert [(item["requested"], item["served"]) for item in found] == (
        [("claude-opus-5-5", served)] if substituted else [])


def test_a_dated_name_of_the_same_model_is_no_substitution(tmp_path, monkeypatch):
    seq = journal.JOURNAL.last_seq
    run(tmp_path, monkeypatch, [[text("ok"), metadata("claude-haiku-4-5"), EOS]], model="claude-haiku-4-5-20251001")
    assert not [event for event in journal.JOURNAL.query(since=seq, limit=1000)
                if event["kind"] == "upstream.model_substituted"]
