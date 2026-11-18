"""Retool's 402 "This model is not available for your plan." refuses one account one model: the account is held back from
that model alone for an hour, and the turn moves to another account before the client sees anything.

Journal of 2026-11-12/13: three accounts answered every claude-opus-5-5 request with it, the relay returned it as a 502,
the client asked the same account again about ten times and the subagent or the session died, while new conversations
kept going to those accounts."""
import asyncio
import io
import json
import time
import urllib.error
from email.message import Message

import pytest

import apierr
import direct
import journal
import models
import upstream_attempts
from accounts import PLAN_REFUSAL_FIRST, PLAN_REFUSAL_HOLD, AccountDispatcher
from session import Backend as PoolBackend, SessionPool
from test_account_dispatcher import Account, Clock, dispatcher
from test_account_rate_limit import CHAT, _events, _headers, _stream, retool, serve_ok, served, tag, turn  # noqa: F401
from test_native_context_reuse import Backend, run
from test_route_errors import M, acct, acquire_error, entry, pool_config

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
REFUSAL = "This model is not available for your plan."
PLAN_BODY = json.dumps({"statusCode": 402, "error": "Payment Required", "message": REFUSAL})
CREDITS_BODY = json.dumps({"statusCode": 402, "error": "Payment Required", "message": "Insufficient credits"})


def refused(request_):
    raise urllib.error.HTTPError(request_.full_url, 402, "Payment Required", Message(), io.BytesIO(PLAN_BODY.encode()))


def refuses(*names):
    """An account whose plan lacks these models and serves every other one."""
    def answer(request_):
        return refused(request_) if json.loads(request_.data)["model"] in names else serve_ok(request_)
    return answer


def sonnet_turn(conversation):
    parsed = turn(conversation)
    parsed.entry, parsed.model = models.resolve(SONNET)[0], SONNET
    return parsed


def texts(events):
    return [event for event in events if event["type"] == "text"]


def errors(events):
    return [event["error"] for event in events if event["type"] == "error"]


# -- recognition ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    (REFUSAL, True), (REFUSAL.upper(), True), ("This  model is not available\nfor your plan", True), (PLAN_BODY, True),
    ("Web search is not available for your plan.", False), ("Insufficient credits", False), ("Payment Required", False),
    ("", False), (None, False)])
def test_the_plan_text_is_recognised_without_regard_to_case_and_spacing(text, expected):
    assert apierr.is_not_in_plan(text) is expected


def test_an_http_402_with_the_plan_text_is_a_refusal_of_the_account_for_the_model():
    error = direct._api_error(402, PLAN_BODY, _headers())
    assert apierr.is_plan_refusal(error) and not apierr.is_account_quota(error)
    # Where it still reaches the client (no attempt left), it is the 502 the client got before.
    assert (error.status, error.code, error.message) == (502, "upstream_error", REFUSAL)


@pytest.mark.parametrize("status,body", [(402, CREDITS_BODY), (402, "Payment Required"), (402, ""), (400, PLAN_BODY),
                                         (403, PLAN_BODY), (429, PLAN_BODY), (500, PLAN_BODY), (502, PLAN_BODY)])
def test_no_other_402_and_no_other_status_is_a_plan_refusal(status, body):
    assert not apierr.is_plan_refusal(direct._api_error(status, body, _headers()))


def test_the_transport_never_asks_the_refusing_account_again(tmp_path, monkeypatch):
    posts = []

    def opener(request_, **_kwargs):
        posts.append(request_)
        return refused(request_)

    with pytest.raises(apierr.ApiError) as caught:
        _stream(tmp_path, monkeypatch, opener)
    assert len(posts) == 1 and caught.value.upstream_status == 402 and apierr.is_plan_refusal(caught.value)


# -- the hold: one account, one model; five minutes, an hour when it repeats -----------------------------------------------
def test_a_refusal_holds_the_account_back_from_that_model_alone(capsys):
    account = Account("a")
    value, _ = dispatcher([account])
    before = journal.JOURNAL.last_seq
    until = value.mark_plan_refusal(account, OPUS, request_id="req-1")
    assert set(account.refused_models) == {OPUS} and account.refused_models[OPUS]["until_at"] == until
    # The account stays in the pool for every other model.
    assert account.unavailable is None and value.status()[0]["state"] == "active"
    held = _events("account.model_refused", before)
    assert len(held) == 1 and held[0]["request_id"] == "req-1" and held[0]["level"] == "warn"
    assert held[0]["data"]["account"] == value._tag(account) and held[0]["data"]["model"] == OPUS
    assert held[0]["data"]["until"] == pytest.approx(until, abs=0.01)
    line = capsys.readouterr().out
    assert "исключён для %s" % OPUS in line and "тариф Retool" in line
    assert time.strftime("%H:%M:%S", time.localtime(until)) in line


def hold_of(value, account, model):
    """(pause, repeated) of the hold a refusal starts, as the pool and the journal see it."""
    before = journal.JOURNAL.last_seq
    value.mark_plan_refusal(account, model)
    return account.refused_models[model]["pause"], _events("account.model_refused", before)[-1]["data"]["repeated"]


def test_the_first_refusal_of_a_pair_holds_it_for_five_minutes(capsys):
    account = Account("a")
    value, _ = dispatcher([account])
    started = time.time()
    assert hold_of(value, account, OPUS) == (PLAN_REFUSAL_FIRST, False) and PLAN_REFUSAL_FIRST == 300
    assert started + 300 <= account.refused_models[OPUS]["until_at"] <= time.time() + 300
    line = capsys.readouterr().out
    assert "(пауза 5 мин)" in line and "повторный" not in line


def test_a_refusal_right_after_a_release_holds_the_pair_for_an_hour_and_so_does_each_one_after_it(capsys):
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_plan_refusal(account, OPUS)
    clock.now += PLAN_REFUSAL_FIRST
    value._tick()
    assert account.refused_models == {}  # released: the next request of the model asks the account again ...
    clock.now += 10
    assert hold_of(value, account, OPUS) == (PLAN_REFUSAL_HOLD, True) and PLAN_REFUSAL_HOLD == 3600  # ... refused again
    assert "(пауза 60 мин, повторный отказ подряд)" in capsys.readouterr().out
    # The ladder is the pair's own: another model of the same account starts from the short hold.
    assert hold_of(value, account, SONNET) == (PLAN_REFUSAL_FIRST, False)
    # Within the hour after the long hold ended it is long again, also when the scheduler has not released it yet.
    clock.now += PLAN_REFUSAL_HOLD + PLAN_REFUSAL_HOLD - 1
    assert hold_of(value, account, OPUS) == (PLAN_REFUSAL_HOLD, True)


def test_a_refusal_long_after_a_release_starts_from_the_short_hold_again():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    value.mark_plan_refusal(account, OPUS)
    clock.now += PLAN_REFUSAL_FIRST
    value._tick()
    assert set(value.state[account]["plan_released"]) == {OPUS}  # when the hold ended is kept for the ladder ...
    clock.now += PLAN_REFUSAL_HOLD
    value._tick()
    assert set(value.state[account]["plan_released"]) == {OPUS}  # ... while a refusal still counts as repeated ...
    clock.now += 1
    value._tick()
    assert value.state[account]["plan_released"] == {}  # ... and no longer
    assert hold_of(value, account, OPUS) == (PLAN_REFUSAL_FIRST, False)


def test_turns_that_fail_together_share_one_hold_and_another_model_gets_its_own():
    account = Account("a")
    value, clock = dispatcher([account])
    before = journal.JOURNAL.last_seq
    first = value.mark_plan_refusal(account, OPUS)
    clock.now += 5
    assert value.mark_plan_refusal(account, OPUS) == first
    value.mark_plan_refusal(account, SONNET)
    assert set(account.refused_models) == {OPUS, SONNET}
    assert [event["data"]["model"] for event in _events("account.model_refused", before)] == [OPUS, SONNET]


def test_the_hold_ends_by_itself_and_another_refusal_holds_the_account_again():
    account = Account("a")
    value, clock = dispatcher([account], session_refresh_interval=0)
    before = journal.JOURNAL.last_seq
    value.mark_plan_refusal(account, OPUS)
    clock.now += 100
    value.mark_plan_refusal(account, SONNET)
    clock.now += PLAN_REFUSAL_FIRST - 100 - 1
    value._tick()
    assert set(account.refused_models) == {OPUS, SONNET}
    clock.now += 2
    value._tick()
    assert set(account.refused_models) == {SONNET}  # each pair's hold is its own
    retried = _events("account.model_retry", before)
    assert [(event["data"]["account"], event["data"]["model"]) for event in retried] == [(value._tag(account), OPUS)]
    clock.now += 100
    value._tick()
    assert account.refused_models == {}
    value.mark_plan_refusal(account, OPUS)
    assert set(account.refused_models) == {OPUS}
    assert len(_events("account.model_refused", before)) == 3


def test_the_real_scheduler_thread_ends_a_hold_by_itself():
    account = Account("a")
    value = AccountDispatcher([account], {"session_refresh_interval": 0, "min_account_balance": 20}, {})
    value.plan_refusal_first = 1.0
    value.start()
    try:
        value.mark_plan_refusal(account, OPUS)
        deadline = time.monotonic() + 8
        while account.refused_models and time.monotonic() < deadline:
            time.sleep(0.05)
        assert account.refused_models == {}
    finally:
        value.stop()


def test_an_unknown_account_or_no_model_is_never_held_and_an_added_account_starts_clean():
    value, _ = dispatcher([Account("a")])
    assert value.mark_plan_refusal(Account("stranger"), OPUS) is None
    assert value.mark_plan_refusal(value.backends[0], None) is None and value.backends[0].refused_models == {}
    late = Account("b")
    value.add_account(late, None)
    assert late.refused_models == {} and value.mark_plan_refusal(late, OPUS) is not None


# -- what the pool makes of it ---------------------------------------------------------------------------------------------
def pool_of(*names, **overrides):
    pool = SessionPool(PoolBackend(pool_config([acct(name, provider_resource_name=M) for name in names], **overrides)))
    return pool, AccountDispatcher(pool.backends, {"session_refresh_interval": 0}, {}, clock=Clock())


def test_the_pool_skips_a_held_account_for_its_model_and_keeps_it_for_the_others():
    pool, value = pool_of("a", "b")
    a, b = pool.backends
    value.mark_plan_refusal(a, OPUS)
    assert pool._candidates(pool.backends, entry(OPUS)) == ("ready", [b])
    assert pool._candidates(pool.backends, entry(SONNET)) == ("ready", [a, b])
    assert not SessionPool._routable(a, entry(OPUS)) and SessionPool._routable(a, entry(SONNET))
    assert pool.unavailable_error(entry(OPUS)) is None and pool.model_refusal(entry(OPUS)) is None


def test_when_every_account_is_held_the_answer_is_a_settled_404_that_names_the_cause():
    pool, value = pool_of("a", "b")
    until = min(value.mark_plan_refusal(backend, OPUS) for backend in pool.backends)
    for error in (pool.unavailable_error(entry(OPUS)), pool.model_refusal(entry(OPUS)), acquire_error(pool, OPUS)):
        assert (error.status, error.code) == (404, "not_found_error") and error.retry_after is None
        assert "model '%s' is not available" % OPUS in error.message
        assert "Retool plan of 2 accounts" in error.message and REFUSAL in error.message
        assert error.message.count("HTTP 402") == 1  # said once for both accounts
        assert "tries again at %s" % time.strftime("%H:%M", time.localtime(until)) in error.message
    assert pool.unavailable_error(entry(SONNET)) is None  # the other models are served as before


def test_a_held_account_beside_one_that_comes_back_by_itself_is_no_settled_refusal():
    pool, value = pool_of("a", "b")
    a, b = pool.backends
    value.mark_plan_refusal(a, OPUS)
    value.mark_rate_limited(b)
    error = pool.unavailable_error(entry(OPUS))
    assert (error.status, error.code) == (429, "rate_limit_error")  # b may serve it again soon: no 404


def test_shared_mode_refuses_the_model_of_its_held_account_with_the_settled_404():
    pool, value = pool_of("a", session_mode="shared", thread_id="shared-thread")
    value.mark_plan_refusal(pool.backends[0], OPUS)
    assert acquire_error(pool, OPUS).status == 404


# -- the turn moves ----------------------------------------------------------------------------------------------------------
def test_a_turn_the_plan_refuses_moves_to_the_next_account_and_the_client_sees_one_answer(retool):
    retool.script["acc-a"] = refused
    checked = []
    retool.dispatcher.check_balance_now = lambda backend: checked.append(backend) or False
    before = journal.JOURNAL.last_seq
    holder = {}
    events = asyncio.run(run(retool.runner, turn(), holder))
    assert retool.posts == ["acc-a", "acc-b"]
    # Nothing of the refused attempt reaches the client, and nothing reaches it twice.
    assert texts(events) == [{"type": "text", "delta": "OK"}] and errors(events) == []
    assert [event for event in events if event["type"] == "end"] == [{"type": "end", "reason": "stop"}]
    assert events[-1] == {"type": "end", "reason": "stop"}
    # The refused attempt generated nothing: the request's speed still counts its output (0 + 1 token).
    assert list(holder["_speed_attempts"].values()) == [0, 1]
    assert set(retool.a.refused_models) == {OPUS} and retool.a.unavailable is None and retool.b.refused_models == {}
    assert retool.pool._conversations["conv-1"].backend is retool.b
    assert checked == []  # a plan refusal says nothing about the balance: no balance read
    assert [cost for _, cost in retool.paid] == [0.1]  # the refused attempt cost nothing
    held = _events("account.model_refused", before)
    assert len(held) == 1 and held[0]["request_id"] == "req-conv-1"
    assert (held[0]["data"]["account"], held[0]["data"]["model"]) == (tag("acc-a"), OPUS)
    moved = _events("account.failover", before)
    assert len(moved) == 1 and moved[0]["request_id"] == "req-conv-1"
    assert (moved[0]["data"]["reason"], moved[0]["data"]["model"], moved[0]["data"]["error"]) == (
        "plan_refused", OPUS, "upstream_error")
    migrated = _events("account.migrate", before)
    assert len(migrated) == 1 and migrated[0]["data"]["reason"] == "plan_refused"
    assert (migrated[0]["data"]["from_account"], migrated[0]["data"]["to_account"]) == (tag("acc-a"), tag("acc-b"))
    assert not retool.pool._active


def test_two_refusing_accounts_and_a_third_that_serves(retool, tmp_path):
    third = Backend(tmp_path, base="https://acc-c.invalid", account_id="acc-c", transport="direct")
    third.headers = lambda: {}
    retool.pool.backends.append(third)
    retool.dispatcher.add_account(third, None)
    retool.script["acc-a"] = retool.script["acc-b"] = refused

    async def scenario():
        first = await run(retool.runner, turn())
        # A new conversation of the model goes straight to the account that serves it.
        await run(retool.runner, turn("conv-2"))
        return first

    events = asyncio.run(scenario())
    assert retool.posts == ["acc-a", "acc-b", "acc-c", "acc-c"]
    assert texts(events) == [{"type": "text", "delta": "OK"}] and errors(events) == []
    assert retool.pool._conversations["conv-1"].backend is third
    assert set(retool.a.refused_models) == set(retool.b.refused_models) == {OPUS} and third.refused_models == {}


def test_more_refusing_accounts_than_attempts_still_reach_the_account_that_serves(retool, tmp_path, monkeypatch):
    # Live pool of 2026-11-18: claude-opus-5-5 is in the plan of 13 of 65 accounts. Each refusal used to cost one of the
    # five attempts, so a turn that met five refusing accounts in a row got the refusal as a 502. A refusal is instant and
    # unbilled: it gives its attempt back.
    monkeypatch.setattr(upstream_attempts, "MAX_ATTEMPTS", 2)
    added = []
    for name in ("acc-c", "acc-d"):
        backend = Backend(tmp_path, base="https://%s.invalid" % name, account_id=name, transport="direct")
        backend.headers = lambda: {}
        retool.pool.backends.append(backend)
        retool.dispatcher.add_account(backend, None)
        added.append(backend)
    retool.script["acc-a"] = retool.script["acc-b"] = retool.script["acc-c"] = refused
    holder = {}
    events = asyncio.run(run(retool.runner, turn(), holder))
    assert retool.posts == ["acc-a", "acc-b", "acc-c", "acc-d"]
    assert texts(events) == [{"type": "text", "delta": "OK"}] and errors(events) == []
    assert retool.pool._conversations["conv-1"].backend is added[1]
    assert holder["_attempt_budget"].refunds == 3


def test_a_request_gets_back_at_most_max_refunds_attempts(monkeypatch):
    monkeypatch.setattr(upstream_attempts, "MAX_REFUNDS", 2)
    budget = upstream_attempts.AttemptBudget()
    assert budget.refund() is False  # nothing taken yet: nothing to give back
    for _ in range(2):
        budget.take(lambda: False)
        assert budget.refund() is True and budget.remaining == upstream_attempts.MAX_ATTEMPTS
    budget.take(lambda: False)
    assert budget.refund() is False and budget.remaining == upstream_attempts.MAX_ATTEMPTS - 1
    # A refund gives back the step's attempt, not the POST: `used` counts every POST that left (audit A09-06: after ten
    # refusals and a failure the client was told upstream_attempts=1 and every attempt was numbered 1). It was (1, 2).
    assert (budget.used, budget.refunds) == (3, 2)


def test_the_error_counts_every_post_and_says_how_many_were_given_back():
    budget = upstream_attempts.AttemptBudget()
    numbers = []
    for _ in range(3):
        numbers.append(budget.take(lambda: False))
        budget.refund()
    numbers.append(budget.take(lambda: False))
    assert numbers == [1, 2, 3, 4]  # each POST has a number of its own (diagnostic ids, "attempt%d")
    assert (budget.step_used, budget.remaining) == (1, upstream_attempts.MAX_ATTEMPTS - 1)
    error = budget.attach(apierr.upstream_error("down"))
    assert (error.upstream_attempts, error.upstream_refunded) == (4, 3)


def test_a_refunded_post_does_not_count_against_the_requests_hard_cap():
    budget = upstream_attempts.AttemptBudget(total_cap=2)
    for _ in range(3):
        budget.take(lambda: False)
        assert budget.refund() is True
    budget.take(lambda: False)
    assert budget.remaining == 1 and budget.used == 4
    budget.take(lambda: False)
    assert budget.remaining == 0
    with pytest.raises(apierr.ApiError) as caught:
        budget.take(lambda: False)
    assert caught.value.code == "upstream_attempt_limit" and "(2 attempts)" in caught.value.message


def test_the_client_is_told_every_post_of_a_request_that_met_refusals(retool, tmp_path):
    third = Backend(tmp_path, base="https://acc-c.invalid", account_id="acc-c", transport="direct")
    third.headers = lambda: {}
    retool.pool.backends.append(third)
    retool.dispatcher.add_account(third, None)

    def bad(request_):
        raise urllib.error.HTTPError(request_.full_url, 400, "x", Message(), io.BytesIO(b'{"message": "bad request"}'))

    retool.script["acc-a"] = retool.script["acc-b"] = refused
    retool.script["acc-c"] = bad
    events = asyncio.run(run(retool.runner, turn()))
    assert retool.posts == ["acc-a", "acc-b", "acc-c"]
    error = errors(events)[0]
    assert (error.upstream_attempts, error.upstream_refunded) == (3, 2)
    logged = [event for event in journal.JOURNAL.query(limit=5000) if event["kind"] == "turn.error"
              and event.get("request_id") == "req-conv-1"]
    assert [(event["data"].get("upstream_attempts"), event["data"].get("refunded")) for event in logged][-1] == (3, 2)


def test_a_bound_conversation_of_that_model_moves_at_its_next_request(retool):
    async def scenario():
        await run(retool.runner, turn())
        assert retool.pool._conversations["conv-1"].backend is retool.a
        retool.posts.clear()
        retool.dispatcher.mark_plan_refusal(retool.a, OPUS)
        before = journal.JOURNAL.last_seq
        return before, await run(retool.runner, turn())

    before, events = asyncio.run(scenario())
    assert retool.posts == ["acc-b"]  # the held account is not even asked
    assert texts(events) == [{"type": "text", "delta": "OK"}] and errors(events) == []
    assert retool.pool._conversations["conv-1"].backend is retool.b
    migrated = _events("account.migrate", before)
    assert len(migrated) == 1 and migrated[0]["data"]["reason"] == "plan_refused"


def test_the_held_account_keeps_serving_its_other_models(retool):
    retool.script["acc-a"] = refuses(OPUS)

    async def scenario():
        await run(retool.runner, turn("conv-opus"))
        retool.b.unavailable = {"reason": "paused"}  # only acc-a is left to serve
        return await run(retool.runner, sonnet_turn("conv-sonnet"))

    events = asyncio.run(scenario())
    assert retool.posts == ["acc-a", "acc-b", "acc-a"]
    assert texts(events) == [{"type": "text", "delta": "OK"}]
    assert retool.pool._conversations["conv-sonnet"].backend is retool.a
    assert retool.a.unavailable is None and set(retool.a.refused_models) == {OPUS}


def test_when_every_account_refuses_the_model_the_client_gets_one_settled_404(retool):
    retool.script["acc-a"] = retool.script["acc-b"] = refuses(OPUS)

    async def scenario():
        first = await run(retool.runner, turn("conv-1"))
        posts = list(retool.posts)
        second = await run(retool.runner, turn("conv-2"))
        return first, posts, second, await run(retool.runner, sonnet_turn("conv-3"))

    first, posts, second, other = asyncio.run(scenario())
    assert posts == ["acc-a", "acc-b"]  # each account asked once, within the attempt budget
    for events in (first, second):
        assert texts(events) == [] and len(errors(events)) == 1 and events[-1]["type"] == "error"
        error = errors(events)[0]
        assert (error.status, error.code) == (404, "not_found_error") and REFUSAL in error.message
        assert "Retool plan of 2 accounts" in error.message
    # The second conversation was answered without a request upstream; a model of the plan is served as before.
    assert retool.posts == ["acc-a", "acc-b", "acc-a"] and texts(other) == [{"type": "text", "delta": "OK"}]
    assert retool.a.unavailable is None and retool.b.unavailable is None


def test_without_attempts_left_the_account_is_held_and_the_client_gets_what_it_got_before(retool, monkeypatch):
    monkeypatch.setattr(upstream_attempts, "MAX_ATTEMPTS", 1)
    retool.script["acc-a"] = refused

    async def scenario():
        first = await run(retool.runner, turn())
        posts = list(retool.posts)
        return first, posts, await run(retool.runner, turn())

    first, posts, second = asyncio.run(scenario())
    error = first[-1]["error"]
    assert posts == ["acc-a"] and set(retool.a.refused_models) == {OPUS}
    # acc-b may still serve the model: a 502 the client asks again, and that request goes to acc-b at once.
    assert (error.status, error.code, error.message) == (502, "upstream_error", REFUSAL)
    assert retool.posts == ["acc-a", "acc-b"] and texts(second) == [{"type": "text", "delta": "OK"}]


def test_without_attempts_left_and_no_account_left_the_client_gets_the_settled_404(retool, monkeypatch):
    monkeypatch.setattr(upstream_attempts, "MAX_ATTEMPTS", 1)
    retool.dispatcher.mark_plan_refusal(retool.b, OPUS)
    retool.script["acc-a"] = refused
    events = asyncio.run(run(retool.runner, turn()))
    error = events[-1]["error"]
    assert retool.posts == ["acc-a"] and len(errors(events)) == 1
    assert (error.status, error.code) == (404, "not_found_error") and REFUSAL in error.message


def test_after_the_hold_the_account_is_asked_again_and_a_new_refusal_holds_it_again(retool):
    retool.script["acc-a"] = refused

    async def scenario():
        await run(retool.runner, turn("conv-1"))
        first = retool.a.refused_models[OPUS]["pause"]
        retool.clock.now += first + 1
        assert retool.dispatcher.release_plan_refusals() and retool.a.refused_models == {}
        # acc-b's plan served the model and acc-a's never did: a new conversation goes to acc-b first, until acc-b carries
        # more than twice acc-a's load plus one (SessionPool._plan_order, design D4 1.6). It was offered to the refusing
        # account first, being the least loaded one (audit A03-03). acc-b takes one more conversation here ...
        await run(retool.runner, turn("conv-x"))
        # ... and the next one asks acc-a again.
        await run(retool.runner, turn("conv-2"))
        again = retool.a.refused_models[OPUS]["pause"]
        retool.script["acc-a"] = serve_ok  # the plan includes the model now
        retool.clock.now += again + 1
        retool.dispatcher.release_plan_refusals()
        await run(retool.runner, turn("conv-3"))
        return first, again

    # Five minutes, then an hour: the account refused again within the hour after its first hold ended.
    assert asyncio.run(scenario()) == (PLAN_REFUSAL_FIRST, PLAN_REFUSAL_HOLD)
    assert retool.posts == ["acc-a", "acc-b", "acc-b", "acc-a", "acc-b", "acc-a"]
    assert retool.pool._conversations["conv-3"].backend is retool.a
    # The conversations that moved stay where their cache is.
    assert retool.pool._conversations["conv-1"].backend is retool.b


@pytest.mark.parametrize("status,body", [(402, CREDITS_BODY), (402, ""), (502, json.dumps({"message": "Bad gateway"}))])
def test_another_402_or_a_502_is_handled_as_before(retool, status, body):
    def answer(request_):
        raise urllib.error.HTTPError(request_.full_url, status, "x", Message(), io.BytesIO(body.encode()))

    retool.script["acc-a"] = answer
    checked = []
    retool.dispatcher.check_balance_now = lambda backend: checked.append(backend) or False
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    error = events[-1]["error"]
    # The balance is read, it is fine, so the request does not move and the client gets the upstream error.
    assert retool.posts == ["acc-a"] and checked == [retool.a]
    assert (error.status, error.code) == (502, "upstream_error") and not apierr.is_plan_refusal(error)
    assert retool.a.refused_models == {} and retool.a.unavailable is None
    assert not _events("account.model_refused", before) and not _events("account.failover", before)


# -- what the client and the operator see ------------------------------------------------------------------------------------
def test_the_client_never_sees_the_refusal_and_the_admin_shows_the_held_model(served):
    served.script["acc-a"] = refused
    status, body, _ = served.call("/v1/chat/completions", CHAT)
    assert status == 200 and body["choices"][0]["message"]["content"] == "OK"
    assert served.posts == ["acc-a", "acc-b"]
    held = served.backends[0]
    assert set(held.refused_models) == {OPUS} and held.unavailable is None
    status, pool, _ = served.call("/admin/api/accounts")
    first, second = pool["accounts"]
    assert (first["state"], first["routable"]) == ("active", True)
    assert first["refused_models"] == {OPUS: held.refused_models[OPUS]["until_at"]}
    assert "refused_models" not in second
    status, health, _ = served.call("/health", key=None)
    assert status == 200 and health["accounts"]["routable"] == 2  # the account still serves the other models


def test_when_every_account_refuses_the_http_answer_is_a_real_404_also_for_a_stream(served):
    served.script["acc-a"] = served.script["acc-b"] = refused
    status, body, _ = served.call("/v1/chat/completions", CHAT)
    assert status == 404 and served.posts == ["acc-a", "acc-b"]
    assert body["error"]["code"] == "not_found_error" and REFUSAL in body["error"]["message"]
    assert OPUS in body["error"]["message"]
    status, body, _ = served.call("/v1/messages", {**CHAT, "max_tokens": 64, "stream": True})
    assert status == 404 and body["error"]["type"] == "not_found_error" and REFUSAL in body["error"]["message"]
    assert served.posts == ["acc-a", "acc-b"]  # answered by the relay, before the stream and without a request upstream


def test_a_streaming_client_gets_one_clean_stream_from_the_account_that_serves(served):
    served.script["acc-a"] = refused
    status, body, _ = served.call("/v1/messages", {**CHAT, "max_tokens": 64, "stream": True})
    assert status == 200 and served.posts == ["acc-a", "acc-b"]
    assert body.count("event: message_start") == 1 and body.count("event: message_stop") == 1
    assert body.count('"OK"') == 1 and "event: error" not in body and REFUSAL not in body
