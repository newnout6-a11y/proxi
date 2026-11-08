"""An hourly-limit refusal inside an HTTP 200 stream costs nothing, and an attempt that reports no token counts never sets
the conversation's prefix size to zero (lens-credits-3).

Retool can send its hourly limit as an error event in a 200 (direct._error_from_event). Before anything was generated that is
the same refusal as the HTTP 429 form, which is free; only the short 429 was treated so. The in-stream form was taken for an
attempt that may have been billed: the account's balance was marked unreported, the client was told an unknown charge
(credits_complete false), and the relay's empty usage event for it reached the orchestrator, which wrote the 0 of that empty
snapshot into _last_prefix_tokens. With account_rate_limit_wait set, the 0 sent the conversation to another account at once,
which writes its whole prefix again (79k-580k tokens): the very rewrite the wait is for. Any other attempt without counts (a
timeout or a cut stream on a 200, a credits event with no tokens) zeroed the size the same way, so the next hourly limit
skipped the wait even in its HTTP form, and the pool took a large conversation for the cheapest to evict."""
import asyncio
from types import SimpleNamespace

import pytest

import apierr
from test_account_rate_limit import MESSAGE, _events, retool, serve_ok, turn  # noqa: F401 - retool is a fixture
from test_account_rate_limit_wait import answered, limited, pauses, second_turn  # noqa: F401 - pauses is a fixture
from test_native_context_reuse import run
from test_review_fixes_pr19 import response, text
from test_usage_truth import MARKER, drive

HOURLY_IN_STREAM = {"event": "error", "statusCode": 429, "error": {"message": MESSAGE}}
THINKING_START = {"event": "content_block_start", "type": "thinking"}


def in_stream(times):
    """An account that answers the hourly limit `times` times inside an HTTP 200, before anything else, then serves."""
    left = {"times": times}

    def script(request_):
        if left["times"] > 0:
            left["times"] -= 1
            return response(HOURLY_IN_STREAM)
        return serve_ok(request_)

    return script


def watch(retool):
    """Spy on what the relay tells the account's balance; returns the list of mark_usage_unknown calls."""
    unknown = []
    retool.runner.credits = SimpleNamespace(record_turn=lambda key, cost: retool.paid.append((key, cost)),
                                            mark_usage_unknown=lambda: unknown.append(True))
    return unknown


def unpriced(events):
    return [event for event in events if event["type"] == "usage" and not event.get("usage")]


# -- the refusal itself ----------------------------------------------------------------------------------------------------
def test_the_transport_takes_an_hourly_refusal_that_opens_a_200_for_no_charge(tmp_path, monkeypatch):
    events, error, unknown, _ = drive(tmp_path, monkeypatch, [[HOURLY_IN_STREAM]])
    assert apierr.is_account_quota(error) and error.status == 429
    assert MARKER not in events and unknown == [], "the same refusal as an HTTP 429, which is never a charge"


@pytest.mark.parametrize("before", [[text("par")], [THINKING_START], [text(" ")]], ids=["text", "thinking", "blank-text"])
def test_one_that_comes_after_anything_the_model_gave_stays_a_possible_charge(tmp_path, monkeypatch, before):
    events, error, unknown, _ = drive(tmp_path, monkeypatch, [[*before, HOURLY_IN_STREAM]])
    assert apierr.is_account_quota(error)
    assert events.count(MARKER) == 1 and unknown == [True]


def test_an_hourly_limit_inside_a_200_waits_like_its_http_form_and_keeps_the_conversation(retool, pauses):
    unknown = watch(retool)
    events, session, before = asyncio.run(second_turn(retool, in_stream(1)))
    assert retool.posts == ["acc-a", "acc-a"] and answered(events), "asked again on its own account after the wait"
    assert pauses == [30.0]
    assert retool.pool._conversations["conv-1"] is session and session.backend is retool.a
    assert len(_events("account.rate_limit_wait", before)) == 1 and not _events("account.failover", before)
    # The refused attempt cost nothing: no unknown charge for the balance, none for the client.
    assert unknown == [] and unpriced(events) == []
    assert [cost for _, cost in retool.paid][-1:] == [0.1]


def test_without_the_wait_it_moves_at_once_and_is_still_no_unknown_charge(retool, pauses):
    unknown = watch(retool)
    events, session, before = asyncio.run(second_turn(retool, in_stream(1), wait=0))
    assert retool.posts == ["acc-a", "acc-b"] and answered(events) and pauses == []
    assert retool.a.unavailable["reason"] == "rate_limited"
    assert unknown == [] and unpriced(events) == []
    assert session._last_prefix_tokens == 50000, "the refusal said nothing of the prefix"


def test_an_hourly_limit_after_streamed_text_may_have_been_billed_and_stays_unknown(retool, pauses):
    unknown = watch(retool)

    def partial(_request):
        return response(text("par"), HOURLY_IN_STREAM)

    events, session, before = asyncio.run(second_turn(retool, partial))
    assert {"type": "text", "delta": "par"} in events and events[-1]["type"] == "error"
    assert unknown == [True] and len(unpriced(events)) == 1
    assert retool.a.unavailable["reason"] == "rate_limited"
    assert session._last_prefix_tokens == 50000


# -- attempts that report no token counts ------------------------------------------------------------------------------------
def test_a_cut_stream_on_a_200_keeps_the_prefix_size_and_the_next_hourly_limit_still_waits(retool, pauses):
    unknown = watch(retool)

    async def story():
        events, session, before = await second_turn(retool, lambda _request: response(text("par")))
        cut = (events, session._last_prefix_tokens)
        retool.posts.clear()
        retool.script["acc-a"] = limited(1)
        return cut, await run(retool.runner, turn()), session

    (cut_events, size_after_cut), events, session = asyncio.run(story())
    assert cut_events[-1]["type"] == "error" and len(unpriced(cut_events)) == 1 and unknown == [True]
    assert size_after_cut == 50000, "an empty usage snapshot is no prefix of 0 tokens"
    assert retool.posts == ["acc-a", "acc-a"] and answered(events) and pauses == [30.0]
    assert session.backend is retool.a


def test_a_credits_event_without_token_counts_keeps_the_prefix_size(retool, pauses):
    priced_only = [text("OK"), {"event": "metadata", "usage": {"creditsUsed": 0.2}}, {"event": "end_of_stream"}]
    events, session, before = asyncio.run(second_turn(retool, lambda _request: response(*priced_only)))
    assert answered(events) and any(event["type"] == "credits" for event in events)
    assert session._last_prefix_tokens == 50000


def test_a_snapshot_with_counts_still_sets_the_size_of_the_prefix(retool, pauses):
    counted = [text("OK"), {"event": "metadata", "usage": {"inputTokens": 7, "promptTokensReadFromCache": 61000,
                                                           "promptTokensWrittenToCache": 900, "outputTokens": 1,
                                                           "creditsUsed": 0.3}}, {"event": "end_of_stream"}]
    events, session, before = asyncio.run(second_turn(retool, lambda _request: response(*counted)))
    assert answered(events) and session._last_prefix_tokens == 61907
    assert session._count_anchor[1] == 61907


def test_the_pool_still_ranks_a_conversation_by_the_prefix_it_last_reported(retool, pauses):
    events, session, before = asyncio.run(second_turn(retool, lambda _request: response(text("par"))))
    assert session._last_prefix_tokens == 50000
    assert retool.pool._rebuild_cost("conv-1") >= 50000, "a large conversation is not the cheapest to evict"
