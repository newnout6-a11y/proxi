"""A route alias spreads its turns as its strategy says (routes[].strategy): "priority" takes its first target that has a
usable account, "weighted" and "least-inflight" choose among the usable accounts of all its targets.

Audit A16-03: the strategy was saved, validated and shown in the admin, and never applied: every turn went to the first
target while it had a usable account."""
import asyncio

import pytest

import models
from accounts import AccountDispatcher
from session import Backend, SessionPool
from test_account_dispatcher import Clock
from test_route_alias_targets import MINARA, MINARA_OPUS, OPUS, RETOOL, load


@pytest.mark.parametrize("strategy,expected", [("priority", "retool"), ("least-inflight", "minara")])
def test_a_route_spreads_its_turns_over_its_targets_as_its_strategy_says(tmp_path, monkeypatch, strategy, expected):
    cfg = load(tmp_path, monkeypatch, [RETOOL, MINARA],
               [{"id": "smart-opus", "targets": [OPUS, MINARA_OPUS], "strategy": strategy}])
    pool = SessionPool(Backend(cfg))
    retool_account, minara_account = pool.backends
    for backend in pool.backends:
        backend.unavailable, backend.refused_models = None, {}
    alias = models.resolve("smart-opus", cfg=cfg)[0]

    async def scenario():
        held = [await pool.acquire(None, model_entry=alias, exclude=(minara_account,)) for _ in range(2)]
        lease = await pool.acquire(None, model_entry=alias)
        for item in (*held, lease):
            await pool.release(item)
        return lease

    lease = asyncio.run(scenario())
    # Two turns are in flight on the Retool account: least-inflight sends the third to the Minara target.
    assert lease.session.backend is {"retool": retool_account, "minara": minara_account}[expected]
    assert lease.bound_entry["id"] == {"retool": OPUS, "minara": MINARA_OPUS}[expected]


def test_a_weighted_route_offers_the_accounts_of_every_target(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, [RETOOL, MINARA],
               [{"id": "smart-opus", "targets": [OPUS, MINARA_OPUS], "strategy": "weighted"}])
    pool = SessionPool(Backend(cfg))
    for backend in pool.backends:
        backend.unavailable, backend.refused_models = None, {}
    alias = models.resolve("smart-opus", cfg=cfg)[0]
    assert pool._candidates(pool.backends, alias) == ("ready", list(pool.backends))
    assert {pool._next_backend(alias).cfg.get("upstream", "retool") for _ in range(40)} == {"retool", "minara"}


@pytest.mark.parametrize("strategy", ["weighted", "least-inflight"])
def test_a_retool_target_that_served_does_not_take_the_turns_of_a_spreading_route(tmp_path, monkeypatch, strategy):
    """The plans' preference (SessionPool._plan_order) orders the accounts of one target. Applied over all the targets of
    a route, a single served turn on Retool sent every turn there: Minara accounts never count as served (review of
    impl/routing-pool: weighted {'retool': 200}, least-inflight against its own rule)."""
    cfg = load(tmp_path, monkeypatch, [RETOOL, MINARA],
               [{"id": "smart-opus", "targets": [OPUS, MINARA_OPUS], "strategy": strategy}])
    pool = SessionPool(Backend(cfg))
    retool_account, minara_account = pool.backends
    dispatcher = AccountDispatcher(pool.backends, {"session_refresh_interval": 0}, {}, clock=Clock())
    for backend in pool.backends:
        backend.unavailable = None
    alias = models.resolve("smart-opus", cfg=cfg)[0]
    dispatcher.mark_plan_served(retool_account, OPUS)
    if strategy == "weighted":
        picks = [pool._next_backend(alias) for _ in range(200)]
        assert 40 < picks.count(minara_account) < 160  # equal weights
    else:
        pool._conversations["idle"] = type("S", (), {"backend": retool_account})()  # Retool carries one conversation
        assert {pool._next_backend(alias) for _ in range(10)} == {minara_account}
