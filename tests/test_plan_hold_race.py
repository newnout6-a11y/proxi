"""acquire() awaits between choosing an account and handing out the lease: the old session's aclose(), the stored state of a
new conversation (_restore_state), a new thread. A request of the same wave that gets Retool's 402 on that account then
leaves a plan hold on it, and the request that was choosing got a settled 404 with attempts and serving accounts left
(journal 2026-11-18 17:08:48-56, requests a6280a19de35, 1af34b1ad462, 90c319c79e2e; design D4 1.7)."""
import asyncio

import pytest

import apierr
import journal
from session import Backend as PoolBackend, RelaySession, SessionPool
from test_account_rate_limit import _events
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
