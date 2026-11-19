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


# -- review of impl/routing-pool -------------------------------------------------------------------------------------------
def step(clock, wall, seconds):
    clock.now += seconds
    wall.now += seconds


def test_a_wave_ends_two_minutes_after_its_last_serving_refusal_and_the_plans_that_lack_the_model_are_written_down(
        tmp_path):
    """Three accounts that served Opus refuse it together once (a fault of Retool's); afterwards the clients' requests meet
    the six accounts whose plans lack Opus, one refusal a minute. Their refusals kept the wave on for hours: each was
    asked every 5 minutes (36 refusals in 3 h) and none was written down; without the fault each is asked twice."""
    servers = [Account("s%d" % index) for index in range(3)]
    refusers = [Account("r%d" % index) for index in range(6)]
    value, clock, wall = plans_dispatcher(tmp_path, servers + refusers)
    for account in servers:
        value.mark_plan_served(account, OPUS)
    for account in servers:
        value.mark_plan_refusal(account, OPUS)
    asked = {account.cfg["account_id"]: [] for account in refusers}
    for _ in range(180):
        step(clock, wall, 60)
        value.release_plan_refusals()
        free = [account for account in refusers if OPUS not in account.refused_models]
        if free:
            value.mark_plan_refusal(free[0], OPUS)
            asked[free[0].cfg["account_id"]].append(free[0].refused_models[OPUS].get("persisted", False))
    assert all(items == [False, True] for items in asked.values()), asked
    assert value._plan_waves[OPUS] < wall.now


def test_a_pair_refused_in_a_wave_is_written_down_at_its_next_refusal_after_it(tmp_path):
    """Design D4 1.10: a real change of three plans at once looks like a wave; the first refusal after it is written."""
    servers = [Account("s%d" % index) for index in range(3)]
    refuser = Account("r")
    value, clock, wall = plans_dispatcher(tmp_path, servers + [refuser])
    for account in servers:
        value.mark_plan_served(account, OPUS)
    for account in [*servers, refuser]:
        value.mark_plan_refusal(account, OPUS)  # the wave, and the refuser asked in the same minute
    assert {account.refused_models[OPUS]["pause"] for account in [*servers, refuser]} == {PLAN_REFUSAL_FIRST}
    step(clock, wall, PLAN_REFUSAL_FIRST + 600)  # the wave is long over, the holds ended 10 minutes ago
    value.release_plan_refusals()
    for account in [refuser, servers[0]]:
        value.mark_plan_refusal(account, OPUS)
        assert account.refused_models[OPUS]["persisted"] is True


def test_a_refusal_written_a_moment_before_the_wave_showed_itself_is_taken_back_and_the_pair_stays_served(tmp_path):
    servers = [Account("s%d" % index) for index in range(3)]
    value, clock, wall = plans_dispatcher(tmp_path, servers)
    for account in servers:
        value.mark_plan_served(account, OPUS)
        value.mark_plan_refusal(account, OPUS)  # a first wave
    value.flush_plans()
    step(clock, wall, PLAN_REFUSAL_FIRST + 60)
    value.release_plan_refusals()
    value.mark_plan_refusal(servers[0], OPUS)  # the fault again: alone, a repeated refusal, written down ...
    assert servers[0].refused_models[OPUS]["persisted"] is True
    value.flush_plans()
    assert on_disk(tmp_path)["accounts"]["s0"]["models"][OPUS]["state"] == "refused"
    for account in servers[1:]:
        value.mark_plan_refusal(account, OPUS)  # ... until the two others show the wave
    assert {account.refused_models[OPUS]["pause"] for account in servers} == {PLAN_REFUSAL_FIRST}
    assert not any(account.refused_models[OPUS].get("persisted") for account in servers)
    value.flush_plans()
    record = on_disk(tmp_path)["accounts"]["s0"]["models"][OPUS]
    assert record["state"] == "served" and retool_plans.epoch(record["served_at"]) == pytest.approx(wall.now - 360)


def test_a_pair_that_serves_all_the_time_is_written_down_again_every_hour(tmp_path):
    """The record was written again only after an hour without a turn: after 6 h of turns every 30 minutes it was 6 h
    old, and after a restart such accounts did not count as serving (the wave rule asks for a turn within a day)."""
    account = Account("a")
    value, clock, wall = plans_dispatcher(tmp_path, [account])
    for _ in range(12):
        value.mark_plan_served(account, OPUS)
        value.flush_plans()
        step(clock, wall, 1800)
    record = on_disk(tmp_path)["accounts"]["a"]["models"][OPUS]
    assert wall.now - retool_plans.epoch(record["served_at"]) <= 3600 + 1800


def test_after_a_restart_the_accounts_that_served_still_make_a_wave_and_are_not_written_down(tmp_path):
    servers = [Account("s%d" % index) for index in range(3)]
    value, clock, wall = plans_dispatcher(tmp_path, servers)
    for _ in range(2 * 24 * 6):  # two days of turns every 10 minutes
        for account in servers:
            value.mark_plan_served(account, OPUS)
        value.flush_plans()
        step(clock, wall, 600)
    clock = Clock()  # the relay restarts
    restarted = AccountDispatcher(servers, {"session_refresh_interval": 0, "_config_path": str(tmp_path / "config.json")},
                                  {}, clock=clock, wall=wall)
    for _ in range(2):  # a fault of Retool's refuses Opus on all three, twice, six minutes apart
        for account in servers:
            restarted.mark_plan_refusal(account, OPUS)
        assert {account.refused_models[OPUS]["pause"] for account in servers} == {PLAN_REFUSAL_FIRST}
        step(clock, wall, PLAN_REFUSAL_FIRST + 60)
        restarted.release_plan_refusals()
    restarted.flush_plans()
    assert {row["models"][OPUS]["state"] for row in on_disk(tmp_path)["accounts"].values()} == {"served"}


def test_a_probe_the_model_began_to_answer_proves_the_plan_includes_it(tmp_path):
    """HTTP 200 and the model answering (it thought through the probe's output limit, no word): a plan without the model
    answers 402 before any of it. It held the pair and asked again every hour, each probe billed."""
    account = Account("a")
    value, clock, wall = plans_dispatcher(tmp_path, [account])
    value.plan_probe = lambda backend, model_entry: {"ok": False, "http": 200, "error": "no answer", "accepted": True,
                                                     "credits": 0.4}
    refuse_twice(value, clock, account)
    clock.now += PLAN_RECHECK + 1
    value.release_plan_refusals()
    assert value.probe_plan(account) == "served"
    assert account.refused_models == {} and OPUS in account.served_models


def test_a_file_another_process_has_open_for_a_moment_keeps_every_record(tmp_path, monkeypatch):
    """A sharing violation (an antivirus, the other relay) when merge read the file: read() took it for a damaged file,
    set it aside as .bad and wrote back only what was being merged."""
    plans = store(tmp_path)
    plans.merge({"acc-%d" % index: {"models": {OPUS: {"state": "refused", "at": retool_plans.stamp(1_800_000_000)}}}
                 for index in range(5)}, now=1_800_000_000)
    real_open = open
    failures = {"left": 1}

    def flaky_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(retool_plans.AUTO_NAME) and "r" in mode and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    plans.merge({"acc-new": {"models": {OPUS: {"state": "served", "at": retool_plans.stamp(1_800_000_001)}}}},
                now=1_800_000_001)
    monkeypatch.setattr("builtins.open", real_open)
    assert len(on_disk(tmp_path)["accounts"]) == 6 and not (tmp_path / (retool_plans.AUTO_NAME + ".bad")).exists()
    # One that stays shut is no damage either: merge fails (flush_plans keeps the records for the next tick).
    failures["left"] = 99
    monkeypatch.setattr("builtins.open", flaky_open)
    with pytest.raises(OSError):
        plans.merge({"acc-x": {"models": {OPUS: {"state": "served", "at": retool_plans.stamp(1_800_000_002)}}}})
    assert plans.read() == {}  # a reader that cannot open it goes on without it, and sets nothing aside
    monkeypatch.setattr("builtins.open", real_open)
    assert len(on_disk(tmp_path)["accounts"]) == 6 and not (tmp_path / (retool_plans.AUTO_NAME + ".bad")).exists()
