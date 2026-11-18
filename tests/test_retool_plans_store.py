"""Retool plan refusals that outlive a restart: retool_plans.auto.json beside config.json (retool_plans.PlanStore), read
by AccountDispatcher at start, written when a refusal repeats, asked again by a background probe after 12-24 h.

Journal of 2026-11-14..18: 1048 refusals of 141 pairs (account, model), each pair asked again about once an hour after
its in-memory hold ended, 74 refusals after each of two restarts on 11-18, and every new conversation of a model went to
the refusing accounts first (audits A03-03, design D4 section 1)."""
import asyncio
import json
import os

import pytest

import journal
import retool_plans
from accounts import (PLAN_PROBE_UNAVAILABLE, PLAN_PROBE_WAIT, PLAN_RECHECK, PLAN_RECHECK_MAX, PLAN_REFUSAL_FIRST,
                      PLAN_SERVED_KEEP, AccountDispatcher)
from session import Backend as PoolBackend, SessionPool
from test_account_dispatcher import Account, Clock
from test_account_rate_limit import _events, retool  # noqa: F401
from test_native_context_reuse import run
from test_retool_plan_refusal import OPUS, SONNET, refused, texts, turn
from test_route_errors import M, acct, entry, pool_config


def store(tmp_path):
    return retool_plans.PlanStore(tmp_path / retool_plans.AUTO_NAME)


def on_disk(tmp_path):
    return json.loads((tmp_path / retool_plans.AUTO_NAME).read_text(encoding="utf-8"))


class Wall:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def plans_dispatcher(tmp_path, accounts, **cfg):
    clock, wall = Clock(), Wall()
    for account in accounts:
        account.cfg.setdefault("provider_resource_name", M)  # a resource: the probe asks on the relay's own route
    settings = {"session_refresh_interval": 0, "_config_path": str(tmp_path / "config.json"), **cfg}
    return AccountDispatcher(accounts, settings, {}, clock=clock, wall=wall), clock, wall


def refuse_twice(value, clock, account, model=OPUS):
    """A refusal, its hold's end, and a refusal within the hour after it: the plan's answer, twice."""
    value.mark_plan_refusal(account, model)
    clock.now += PLAN_REFUSAL_FIRST
    value.wall.now += PLAN_REFUSAL_FIRST
    value.release_plan_refusals()
    clock.now += 10
    value.wall.now += 10
    return value.mark_plan_refusal(account, model)


# -- the file ---------------------------------------------------------------------------------------------------------------
def test_the_file_is_written_atomically_beside_config_json_and_holds_no_secret(tmp_path):
    account = Account("acc-a")
    account.cfg.update(cookie="accessToken=SECRET-COOKIE-0123456789", xsrf="SECRET-XSRF-0123")
    value, clock, _ = plans_dispatcher(tmp_path, [account])
    assert value.plans.path == os.fspath(tmp_path / "retool_plans.auto.json")
    refuse_twice(value, clock, account)
    assert value.flush_plans() is True
    text = (tmp_path / retool_plans.AUTO_NAME).read_text(encoding="utf-8")
    assert "SECRET" not in text and "cookie" not in text.lower() and "xsrf" not in text.lower()
    data = json.loads(text)
    assert data["version"] == retool_plans.VERSION and set(data["accounts"]) == {"acc-a"}
    record = data["accounts"]["acc-a"]["models"][OPUS]
    assert (record["state"], record["refusals"], record["source"]) == ("refused", 1, "chat")
    assert retool_plans.epoch(record["recheck_at"]) == pytest.approx(value.wall.now + PLAN_RECHECK, abs=1)
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]


def test_two_writers_keep_each_others_facts_and_the_newer_record_of_a_pair_wins(tmp_path):
    first, second = store(tmp_path), store(tmp_path)
    first.merge({"a": {"tag": "ta", "models": {OPUS: {"state": "refused", "at": retool_plans.stamp(1_800_000_000)}}}})
    second.merge({"b": {"tag": "tb", "models": {SONNET: {"state": "served", "at": retool_plans.stamp(1_800_000_000)}}}})
    second.merge({"a": {"models": {OPUS: {"state": "served", "at": retool_plans.stamp(1_799_999_000)}}}})  # older: ignored
    accounts = on_disk(tmp_path)["accounts"]
    assert accounts["a"]["models"][OPUS]["state"] == "refused" and accounts["a"]["tag"] == "ta"
    assert accounts["b"]["models"][SONNET]["state"] == "served"


def test_a_damaged_file_is_set_aside_and_the_relay_starts_without_it(tmp_path):
    (tmp_path / retool_plans.AUTO_NAME).write_text("{not json", encoding="utf-8")
    before = journal.JOURNAL.last_seq
    value, _, _ = plans_dispatcher(tmp_path, [Account("a")])
    assert value.backends[0].refused_models == {}
    assert (tmp_path / (retool_plans.AUTO_NAME + ".bad")).exists()
    assert _events("account.plans_unreadable", before)


def test_the_records_of_an_account_gone_from_config_json_are_dropped_after_a_week(tmp_path):
    now = 2_000_000_000.0
    old, recent = retool_plans.stamp(now - retool_plans.FORGET_ACCOUNT - 60), retool_plans.stamp(now - 60)
    store(tmp_path).merge({"gone": {"models": {OPUS: {"state": "refused", "at": old}}},
                           "left": {"models": {OPUS: {"state": "refused", "at": recent}}},
                           "here": {"models": {OPUS: {"state": "refused", "at": old}}}}, known={"here"}, now=now)
    assert set(on_disk(tmp_path)["accounts"]) == {"left", "here"}


# -- the dispatcher ------------------------------------------------------------------------------------------------------
def test_a_first_refusal_stays_in_memory_and_a_repeated_one_is_written_and_held_until_the_recheck(tmp_path):
    account = Account("a")
    value, clock, wall = plans_dispatcher(tmp_path, [account])
    value.mark_plan_refusal(account, OPUS)
    assert value.flush_plans() is False and not (tmp_path / retool_plans.AUTO_NAME).exists()
    assert account.refused_models[OPUS]["pause"] == PLAN_REFUSAL_FIRST
    before = journal.JOURNAL.last_seq
    until = refuse_twice(value, clock, account)
    assert until == pytest.approx(wall.now + PLAN_RECHECK)
    assert account.refused_models[OPUS] == {"until_at": until, "pause": PLAN_RECHECK, "persisted": True}
    held = _events("account.model_refused", before)[-1]
    assert held["data"]["persisted"] is True and "подтверждено повторно" in held["message"]
    value.flush_plans()
    assert on_disk(tmp_path)["accounts"]["a"]["models"][OPUS]["state"] == "refused"


def test_a_new_dispatcher_on_the_same_file_holds_the_pair_at_once_and_the_pool_answers_without_a_request(tmp_path):
    cfg = pool_config([acct(name, provider_resource_name=M) for name in ("a", "b")],
                      _config_path=str(tmp_path / "config.json"))
    pool = SessionPool(PoolBackend(cfg))
    value = AccountDispatcher(pool.backends, cfg, {}, clock=Clock())
    for backend in pool.backends:
        value.mark_plan_refusal(backend, OPUS)
        value.state[backend]["plan_refusals"][OPUS]["until"] = 0  # the short hold is over ...
        value.release_plan_refusals()
        value.mark_plan_refusal(backend, OPUS)  # ... and the plan refuses again: written down
    value.flush_plans()
    # A restart: the holds are known before any request.
    restarted = SessionPool(PoolBackend(cfg))
    AccountDispatcher(restarted.backends, cfg, {}, clock=Clock())
    assert all(set(backend.refused_models) == {OPUS} for backend in restarted.backends)
    error = restarted.model_refusal(entry(OPUS))
    assert error.status == 404 and "re-checks them in the background" in error.message
    assert restarted.model_refusal(entry(SONNET)) is None


def test_the_end_of_a_written_hold_queues_a_probe_instead_of_handing_the_pair_to_a_request(tmp_path):
    account = Account("a")
    value, clock, wall = plans_dispatcher(tmp_path, [account])
    refuse_twice(value, clock, account)
    clock.now += PLAN_RECHECK + 1
    assert value.release_plan_refusals() is False
    assert set(account.refused_models) == {OPUS} and value.state[account]["plan_probe_due"] == {OPUS}
    assert account.refused_models[OPUS]["pause"] == PLAN_PROBE_WAIT
    assert value.due(clock.now) == [(account, "plan_probe")]


@pytest.mark.parametrize("row,outcome,pause", [
    ({"ok": False, "http": 402, "error": "This model is not available for your plan."}, "refused", PLAN_RECHECK_MAX),
    ({"ok": False, "http": 429, "error": "hourly limit"}, "unknown", PLAN_PROBE_WAIT),
    ({"ok": False, "http": 400, "error": "Model is not available right now"}, "unavailable", PLAN_PROBE_UNAVAILABLE),
])
def test_a_probe_that_meets_no_answer_holds_the_pair_longer(tmp_path, row, outcome, pause):
    account = Account("a")
    asked = []
    value, clock, wall = plans_dispatcher(tmp_path, [account])
    value.plan_probe = lambda backend, model_entry: asked.append(model_entry["id"]) or dict(row)
    refuse_twice(value, clock, account)
    clock.now += PLAN_RECHECK + 1
    value.release_plan_refusals()
    assert value.probe_plan(account) == outcome and asked == [OPUS]
    assert account.refused_models[OPUS]["pause"] == pause and value.state[account]["plan_probe_due"] == set()
    value.flush_plans()
    record = on_disk(tmp_path)["accounts"]["a"]["models"][OPUS]
    assert record["checks"] == (1 if outcome == "refused" else 0)


def test_a_probe_that_is_served_ends_the_hold_and_writes_the_pair_down_as_served(tmp_path):
    account = Account("a")
    value, clock, wall = plans_dispatcher(tmp_path, [account])
    value.plan_probe = lambda backend, model_entry: {"ok": True, "http": 200, "error": None}
    refuse_twice(value, clock, account)
    clock.now += PLAN_RECHECK + 1
    value.release_plan_refusals()
    before = journal.JOURNAL.last_seq
    assert value.probe_plan(account) == "served"
    assert account.refused_models == {} and OPUS in account.served_models
    assert _events("account.model_served", before) and _events("account.plan_probe", before)
    value.flush_plans()
    assert on_disk(tmp_path)["accounts"]["a"]["models"][OPUS]["state"] == "served"


def test_the_probes_keep_to_their_hourly_budget_and_skip_an_account_out_of_the_pool(tmp_path):
    accounts = [Account("a"), Account("b"), Account("c")]
    value, clock, wall = plans_dispatcher(tmp_path, accounts, retool_plan_probes=1)
    for account in accounts:
        refuse_twice(value, clock, account)
    clock.now += PLAN_RECHECK + 1
    value.release_plan_refusals()
    accounts[0].unavailable = {"reason": "rate_limited"}
    assert value.due(clock.now) == [(accounts[1], "plan_probe")]  # one an hour; a is in its hourly limit
    value._unclaim(accounts[1], "plan_probe")
    assert value.due(clock.now) == []
    clock.now += 3600
    assert value.due(clock.now) == [(accounts[1], "plan_probe")]


def test_a_wave_of_refusals_from_accounts_that_served_the_model_is_never_written(tmp_path):
    accounts = [Account(name) for name in ("a", "b", "c", "d")]
    value, clock, wall = plans_dispatcher(tmp_path, accounts)
    for account in accounts:
        value.mark_plan_served(account, OPUS)
        value.mark_plan_refusal(account, OPUS)  # the first refusals of a fault of Retool's
    clock.now += PLAN_REFUSAL_FIRST
    wall.now += PLAN_REFUSAL_FIRST
    value.release_plan_refusals()
    clock.now += 30
    wall.now += 30
    before = journal.JOURNAL.last_seq
    for account in accounts:
        value.mark_plan_refusal(account, OPUS)  # ... and the same fault a few minutes later
    assert all(account.refused_models[OPUS]["pause"] == PLAN_REFUSAL_FIRST for account in accounts)
    assert not any(account.refused_models[OPUS].get("persisted") for account in accounts)
    value.flush_plans()
    records = [row["models"].get(OPUS, {}) for row in on_disk(tmp_path)["accounts"].values()]
    assert not [record for record in records if record.get("state") == "refused"]
    assert len(_events("account.model_refused_wave", before)) == 1


def test_at_start_a_record_past_its_recheck_is_held_for_its_probe_and_without_probes_not_at_all(tmp_path):
    wall = Wall()
    store(tmp_path).merge({"a": {"models": {OPUS: {"state": "refused", "at": retool_plans.stamp(wall.now - 99999),
                                                   "recheck_at": retool_plans.stamp(wall.now - 60)}},
                                 }}, now=wall.now)
    account = Account("a")
    value = AccountDispatcher([account], {"_config_path": str(tmp_path / "config.json")}, {}, clock=Clock(), wall=wall)
    assert account.refused_models[OPUS]["pause"] == PLAN_PROBE_WAIT and value.state[account]["plan_probe_due"] == {OPUS}
    other = Account("a")
    AccountDispatcher([other], {"_config_path": str(tmp_path / "config.json"), "retool_plan_probes": 0}, {},
                      clock=Clock(), wall=wall)
    assert other.refused_models == {}


def test_a_served_pair_is_written_at_most_once_an_hour_and_shown_to_the_pool_for_a_week(tmp_path):
    account = Account("a")
    value, clock, wall = plans_dispatcher(tmp_path, [account])
    value.mark_plan_served(account, OPUS)
    assert value.flush_plans() is True
    value.mark_plan_served(account, OPUS)
    assert value.flush_plans() is False  # nothing new to write
    wall.now += PLAN_SERVED_KEEP + 1
    value.mark_plan_served(Account("stranger"), OPUS)
    with value._lock:
        value._publish_plan_refusals(account)
    assert account.served_models == {}


# -- end to end ------------------------------------------------------------------------------------------------------------
def test_after_a_restart_a_new_conversation_goes_to_the_account_that_serves_without_asking_the_refusing_one(
        retool, tmp_path):
    retool.dispatcher.plans = store(tmp_path)
    retool.script["acc-a"] = refused

    async def scenario():
        await run(retool.runner, turn("conv-1"))
        retool.clock.now += PLAN_REFUSAL_FIRST + 1
        retool.dispatcher.release_plan_refusals()
        # acc-a's plan refuses again within the hour (another client's request): written down.
        retool.dispatcher.mark_plan_refusal(retool.a, OPUS)
        retool.dispatcher.flush_plans()
        # The relay restarts: a new dispatcher over the same accounts and file.
        restarted = AccountDispatcher(list(retool.pool.backends), retool.runner.cfg, {}, clock=retool.clock,
                                      plans=store(tmp_path))
        retool.runner.accounts = restarted
        retool.posts.clear()
        return await run(retool.runner, turn("conv-3"))

    events = asyncio.run(scenario())
    assert texts(events) == [{"type": "text", "delta": "OK"}]
    assert retool.posts == ["acc-b"]  # the refusing account was not asked
    accounts = on_disk(tmp_path)["accounts"]
    assert accounts["acc-a"]["models"][OPUS]["state"] == "refused"
    assert accounts["acc-b"]["models"][OPUS]["state"] == "served"
