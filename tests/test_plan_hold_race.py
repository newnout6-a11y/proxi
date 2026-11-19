"""acquire() awaits between choosing an account and handing out the lease: the old session's aclose(), the stored state of a
new conversation (_restore_state), a new thread. A request of the same wave that gets Retool's 402 on that account then
leaves a plan hold on it, and the request that was choosing got a settled 404 with attempts and serving accounts left
(journal 2026-11-18 17:08:48-56, requests a6280a19de35, 1af34b1ad462, 90c319c79e2e; design D4 1.7)."""
import asyncio
from types import SimpleNamespace

import pytest

import apierr
import journal
from accounts import cfg_identity
from orchestrator import PLAN_HOLD_RACES
from session import Backend as PoolBackend, RelaySession, SessionPool
from test_account_rate_limit import _events, retool, turn  # noqa: F401
from test_native_context_reuse import run
from test_retool_plan_refusal import errors, texts
from test_route_errors import M, acct, entry, pool_config

OPUS = "claude-opus-5-5"


def pool_of(*names):
    """Accounts by priority, so that the pool's choice is known: the first name is the first choice."""
    accounts = [acct(name, provider_resource_name=M, priority=index) for index, name in enumerate(names)]
    pool = SessionPool(PoolBackend(pool_config(accounts)))
    for backend in pool.backends:
        backend.unavailable, backend.refused_models = None, {}
    return pool


def hold(backend, model=OPUS):
    backend.refused_models = {**backend.refused_models, model: {"until_at": 2e9, "pause": 300.0}}


def holding_on_close(monkeypatch, *backends):
    """RelaySession.aclose that lets a neighbour's 402 land on `backends` while acquire() waits for it (once)."""
    original = RelaySession.aclose
    pending = list(backends)

    async def aclose(self):
        while pending:
            hold(pending.pop())
        await asyncio.sleep(0)
        return await original(self)

    monkeypatch.setattr(RelaySession, "aclose", aclose)


def lease_of(pool, key, **kwargs):
    async def scenario():
        lease = await pool.acquire(key, model_entry=entry(OPUS), **kwargs)
        await pool.release(lease)
        return lease
    return asyncio.run(scenario())


def test_a_hold_that_appears_while_a_move_awaits_moves_the_conversation_again(monkeypatch):
    pool = pool_of("a", "b", "c")
    a, b, c = pool.backends
    assert lease_of(pool, "conv").session.backend is a
    holding_on_close(monkeypatch, b)
    before = journal.JOURNAL.last_seq
    # The turn failed on a (excluded): the conversation moves to b, and b's plan refuses the model meanwhile.
    lease = lease_of(pool, "conv", exclude=(a,))
    assert lease.session.backend is c and pool._conversations["conv"].backend is c
    moved = _events("account.migrate", before)
    assert [(event["data"]["from_account"], event["data"]["to_account"]) for event in moved] == [
        (journal.tag("a"), journal.tag("b")), (journal.tag("b"), journal.tag("c"))]
    assert moved[1]["data"]["reason"] == "plan_refused"


def test_a_hold_that_appears_while_a_new_conversation_reads_its_state_moves_it(monkeypatch):
    pool = pool_of("a", "b")
    a, b = pool.backends
    original = pool._restore_state

    async def restore(key, session):
        hold(a)  # a 402 of the same wave on the account this conversation was given
        await asyncio.sleep(0)
        return await original(key, session)

    monkeypatch.setattr(pool, "_restore_state", restore)
    lease = lease_of(pool, "conv-new")
    assert lease.session.backend is b and pool._conversations["conv-new"].backend is b


def test_when_every_other_account_is_held_meanwhile_the_answer_is_the_settled_404(monkeypatch):
    pool = pool_of("a", "b", "c")
    a, b, c = pool.backends
    lease_of(pool, "conv")
    hold(a)  # the request's own 402 on a, which it then moved away from
    holding_on_close(monkeypatch, b, c)
    with pytest.raises(apierr.ApiError) as caught:
        lease_of(pool, "conv", exclude=(a,))
    assert (caught.value.status, caught.value.code) == (404, "not_found_error")
    assert "Retool plan of 3 accounts" in caught.value.message
    assert not pool._active and pool._slots._value == pool.cfg["max_workers"]


def test_a_refusal_of_the_configuration_is_still_the_settled_404_without_a_move(monkeypatch):
    pool = pool_of("a", "b")
    a, b = pool.backends
    lease_of(pool, "conv")
    original = RelaySession.aclose

    async def aclose(self):
        b.cfg["provider_resource_name"] = None  # b loses its resource meanwhile: a fact of its configuration
        await asyncio.sleep(0)
        return await original(self)

    monkeypatch.setattr(RelaySession, "aclose", aclose)
    before = journal.JOURNAL.last_seq
    with pytest.raises(apierr.ApiError) as caught:
        lease_of(pool, "conv", exclude=(a,))
    assert caught.value.status == 404 and len(_events("account.migrate", before)) == 1


def holding_while_waiting_for_room(monkeypatch, pool, backend):
    """SessionPool._wait_for_room that lets a neighbour's 402 land on `backend` while acquire() waits there."""
    original = pool._wait_for_room

    async def wait_for_room(key, closing):
        hold(backend)
        await asyncio.sleep(0)
        return await original(key, closing)

    monkeypatch.setattr(pool, "_wait_for_room", wait_for_room)


def test_a_parallel_request_whose_account_is_held_while_it_waits_goes_to_another_account(monkeypatch):
    """The second request of a conversation whose first one is in flight runs on a session beside it: the account chosen
    before acquire() waited (affinity) was checked once more and gave the settled 404 (review of impl/routing-pool)."""
    pool = pool_of("a", "b")
    a, b = pool.backends

    async def scenario():
        first = await pool.acquire("conv", model_entry=entry(OPUS))
        assert first.session.backend is a
        holding_while_waiting_for_room(monkeypatch, pool, a)
        second = await pool.acquire("conv", model_entry=entry(OPUS))
        await pool.release(second)
        await pool.release(first)
        return second

    assert asyncio.run(scenario()).session.backend is b


def test_a_stored_conversation_whose_account_is_held_while_it_waits_goes_to_another_account(monkeypatch):
    """The first request of a conversation stored before a restart returns to its account (_stored_affinity); a 402 of the
    same wave lands on that account while the request waits (the 17:08 restart wave of 2026-11-18)."""
    pool = pool_of("a", "b")
    a, b = pool.backends

    class Store:
        def peek(self, key):
            return SimpleNamespace(account=cfg_identity(a.cfg), key_id="k1") if key == "conv-s" else None

        def skip(self, meta, reason):
            pass

    pool._store = Store()

    async def no_restore(key, session):
        return None

    monkeypatch.setattr(pool, "_restore_state", no_restore)
    holding_while_waiting_for_room(monkeypatch, pool, a)
    assert lease_of(pool, "conv-s").session.backend is b


def test_with_the_one_account_held_meanwhile_the_answer_is_still_the_settled_404(monkeypatch):
    pool = pool_of("a")
    (a,) = pool.backends

    async def scenario():
        first = await pool.acquire("conv", model_entry=entry(OPUS))
        holding_while_waiting_for_room(monkeypatch, pool, a)
        try:
            with pytest.raises(apierr.ApiError) as caught:
                await pool.acquire("conv", model_entry=entry(OPUS))
        finally:
            await pool.release(first)
        return caught.value

    error = asyncio.run(scenario())
    assert error.status == 404 and "Retool plan of 1 account" in error.message


def test_a_404_of_a_hold_says_so_and_one_of_the_configuration_does_not():
    pool = pool_of("a", "b")
    a, b = pool.backends
    hold(a)
    with pytest.raises(apierr.ApiError) as caught:
        SessionPool._check_route(a, entry(OPUS))
    assert caught.value.status == 404 and caught.value.plan_hold is True
    b.cfg["provider_resource_name"] = None
    with pytest.raises(apierr.ApiError) as caught:
        SessionPool._check_route(b, entry(OPUS))
    assert caught.value.status == 404 and not getattr(caught.value, "plan_hold", False)


# -- the orchestrator's safety net (design D4 1.7, fix 2) -------------------------------------------------------------------
def racing_acquire(monkeypatch, retool, races, *held):
    """pool.acquire that, `races` times, answers what SessionPool._check_route says when a hold landed on acc-a while the
    request waited (a 404 marked plan_hold); `held` are the accounts the neighbours' 402s hold meanwhile."""
    original = retool.pool.acquire
    calls = []

    async def acquire(*args, **kwargs):
        calls.append(kwargs.get("exclude"))
        if len(calls) <= races:
            for backend in held:
                hold(backend, kwargs["model_entry"]["id"])
            SessionPool._check_route(retool.a, kwargs["model_entry"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(retool.pool, "acquire", acquire)
    return calls


def test_a_plan_hold_404_from_acquire_is_not_final_while_another_account_serves(retool, monkeypatch):
    calls = racing_acquire(monkeypatch, retool, 1, retool.a)
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(retool.runner, turn()))
    assert texts(events) == [{"type": "text", "delta": "OK"}] and errors(events) == []
    assert retool.posts == ["acc-b"] and len(calls) == 2
    assert [event["data"]["reason"] for event in _events("account.failover", before)] == ["plan_hold_race"]
    assert [event["level"] for event in _events("turn.error", before)] == ["warn"]  # the client saw none of it


def test_a_plan_hold_404_is_final_when_every_account_is_held_and_the_request_chooses_again_three_times_at_most(
        retool, monkeypatch):
    racing_acquire(monkeypatch, retool, 1, retool.a, retool.b)
    events = asyncio.run(run(retool.runner, turn()))
    assert [error.status for error in errors(events)] == [404] and retool.posts == []
    retool.b.refused_models = {}
    calls = racing_acquire(monkeypatch, retool, 99, retool.a)  # acc-a alone is held, and the race comes back each time
    events = asyncio.run(run(retool.runner, turn("conv-2")))
    assert [error.status for error in errors(events)] == [404] and len(calls) == 1 + PLAN_HOLD_RACES
