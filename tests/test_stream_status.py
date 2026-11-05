"""A stream is refused with the same status, code and Retry-After as a request that is not streamed."""
import asyncio
import time

import pytest

import apierr
from test_serving_accounts import (DIALECTS, FIRST_LACKS_GLM, GLM, M, account, entry, pool_of,  # noqa: F401 - fixture
                                   relay_pool)

OPUS = "claude-opus-5-5"
TWO_ACCOUNTS = [account("acc-a", provider_resource_name=M), account("acc-b", provider_resource_name=M)]
PARKED = {"reason": "rate_limited", "pause": 900.0}


def parked(seconds=600):
    return dict(PARKED, until_at=time.time() + seconds)


def park(runtime, *backends):
    for backend in backends or runtime.accounts.backends:
        runtime.accounts.mark_rate_limited(backend)


def exhaust(runtime, *backends, available=0.0):
    for backend in backends or runtime.accounts.backends:
        runtime.accounts.managers[backend].balance["available"] = available
        runtime.accounts.check_balance(backend)


def answer(call, dialect, stream, model=OPUS, tools=False, headers=None):
    path, build = DIALECTS[dialect]
    status, body, response_headers = call(path, dict(build(model, tools), stream=stream), headers)
    error = body.get("error") if isinstance(body, dict) else None
    return {"status": status, "type": (error or {}).get("type"), "message": (error or {}).get("message"),
            "retry_after": response_headers.get("retry-after"), "body": body}


def both(call, dialect, **kwargs):
    return answer(call, dialect, False, **kwargs), answer(call, dialect, True, **kwargs)


# -- the three scenarios of the review, in each dialect -------------------------------------------------------
@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_every_account_parked_is_the_same_429_with_retry_after_for_a_stream(relay_pool, dialect):
    call = relay_pool(TWO_ACCOUNTS)
    park(call.runtime)
    plain, streamed = both(call, dialect)
    assert plain["status"] == streamed["status"] == 429
    assert plain["type"] == streamed["type"] == "rate_limit_error"
    assert plain["message"] == streamed["message"]
    assert "все аккаунты упёрлись в часовой лимит Retool" in plain["message"]
    assert plain["retry_after"] == streamed["retry_after"]
    assert plain["retry_after"].isdigit() and 890 <= int(plain["retry_after"]) <= 900
    assert call.posts == []


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_every_account_exhausted_is_the_same_402_for_a_stream(relay_pool, dialect):
    call = relay_pool(TWO_ACCOUNTS)
    exhaust(call.runtime)
    plain, streamed = both(call, dialect)
    assert plain["status"] == streamed["status"] == 402
    assert plain["type"] == streamed["type"] == "insufficient_credits"
    assert plain["message"] == streamed["message"] and "Баланс Retool закончился" in streamed["message"]
    assert plain["retry_after"] is None and streamed["retry_after"] is None
    assert call.posts == []


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_a_parked_account_does_not_stop_a_stream_another_account_can_serve(relay_pool, dialect):
    call = relay_pool(TWO_ACCOUNTS)
    park(call.runtime, call.runtime.accounts.backends[0])
    plain, streamed = both(call, dialect)
    assert plain["status"] == streamed["status"] == 200
    assert call.posts == ["acc-b", "acc-b"]


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_every_account_switched_off_is_the_same_503_for_a_stream(relay_pool, dialect):
    call = relay_pool(TWO_ACCOUNTS)
    for backend in call.runtime.accounts.backends:
        backend.cfg["enabled"] = False
    plain, streamed = both(call, dialect)
    assert plain["status"] == streamed["status"] == 503
    assert plain["type"] == streamed["type"] == "service_unavailable"
    assert plain["message"] == streamed["message"]


def test_parked_and_exhausted_accounts_together_are_a_429_for_both(relay_pool):
    call = relay_pool(TWO_ACCOUNTS)
    first, second = call.runtime.accounts.backends
    park(call.runtime, first)
    exhaust(call.runtime, second)
    plain, streamed = both(call, "chat")
    assert plain["status"] == streamed["status"] == 429 and "1 из 2" in streamed["message"]
    assert plain["retry_after"] == streamed["retry_after"]


# -- what acquire() would still serve is not refused -----------------------------------------------------------
def test_an_exhausted_account_with_credits_left_is_the_failover_reserve(relay_pool):
    call = relay_pool(TWO_ACCOUNTS)
    exhaust(call.runtime, available=5.0)  # below min_account_balance (20), still positive
    plain, streamed = both(call, "chat")
    assert plain["status"] == streamed["status"] == 200


def test_a_signed_out_account_is_still_tried(relay_pool):
    call = relay_pool(TWO_ACCOUNTS[:1])
    call.runtime.accounts.backends[0].unavailable = {"reason": "auth_dead", "error": "synthetic"}
    assert answer(call, "chat", True)["status"] == 200 and call.posts == ["acc-a"]


def test_a_top_up_is_adopted_before_a_stream_is_refused_with_402(relay_pool):
    call = relay_pool(TWO_ACCOUNTS)
    exhaust(call.runtime)
    for backend in call.runtime.accounts.backends:
        call.runtime.accounts.managers[backend].balance["available"] = 500.0
    assert answer(call, "chat", True)["status"] == 200
    assert all(backend.unavailable is None for backend in call.runtime.accounts.backends)


def test_a_conversation_may_move_when_its_own_account_is_parked(relay_pool):
    call = relay_pool(TWO_ACCOUNTS)
    chat = {"x-session-affinity": "moving-chat"}
    assert answer(call, "chat", False, headers=chat)["status"] == 200
    bound = call.posts[-1]
    owner = next(b for b in call.runtime.accounts.backends if b.cfg["account_id"] == bound)
    park(call.runtime, owner)
    assert answer(call, "chat", True, headers=chat)["status"] == 200
    assert call.posts[-1] != bound


# -- the pool's own answer ------------------------------------------------------------------------------------
def test_the_pool_answers_without_reserving_anything_or_waiting():
    pool = pool_of(TWO_ACCOUNTS, session_mode="per_conversation")
    first, second = pool.backends
    opus = entry(OPUS)

    def footprint():
        return pool._slots._value, dict(pool._conversations), dict(pool._active), getattr(pool, "_rr_index", None)

    before = footprint()
    assert pool.unavailable_error(opus) is None
    first.unavailable = second.unavailable = parked(120)
    error = pool.unavailable_error(opus)
    assert (error.status, error.code) == (429, "rate_limit_error") and 110 <= error.retry_after <= 120
    first.unavailable = {"reason": "exhausted", "available": 0.0, "threshold": 20.0}
    second.unavailable = {"reason": "exhausted", "available": -1.0, "threshold": 20.0}
    assert pool.unavailable_error(opus).status == 402
    first.unavailable = second.unavailable = None
    assert footprint() == before


@pytest.mark.parametrize("state", [parked(600), {"reason": "exhausted", "available": 0.0, "threshold": 20.0},
                                   {"reason": "paused"}])
def test_the_pool_predicts_the_error_acquire_raises(state):
    pool = pool_of(TWO_ACCOUNTS, session_mode="per_conversation")
    for backend in pool.backends:
        backend.unavailable = state
    predicted = pool.unavailable_error(entry(OPUS))

    async def acquire():
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire("chat", model_entry=entry(OPUS))
        return caught.value

    actual = asyncio.run(acquire())
    assert (predicted.status, predicted.code, predicted.message) == (actual.status, actual.code, actual.message)


def test_the_check_follows_the_accounts_of_the_requests_own_path():
    pool = pool_of(TWO_ACCOUNTS, session_mode="shared")
    pool.backends[0].unavailable = parked()
    opus = entry(OPUS)
    # The shared session only has the first account; a text chat or a standalone job may use the second.
    assert pool.unavailable_error(opus).status == 429
    assert pool.unavailable_error(opus, isolated=True) is None
    assert pool.unavailable_error(opus, context_scope="text") is None


def test_a_pool_without_any_resource_leaves_the_missing_resource_to_acquire():
    pool = pool_of([account("acc-a"), account("acc-b")], session_mode="per_conversation")
    assert pool.unavailable_error(entry(OPUS)) is None
    for backend in pool.backends:
        backend.unavailable = parked(60)
    assert pool.unavailable_error(entry(OPUS)).status == 429


def test_a_parked_account_that_alone_has_the_resource_is_a_429_not_the_404_of_a_model_nobody_serves():
    pool = pool_of(FIRST_LACKS_GLM, session_mode="per_conversation")
    pool.backends[1].unavailable = parked(60)
    assert pool.unavailable_error(entry(GLM)).status == 429
    assert pool.model_refusal(entry(GLM)) is None
