"""Keepwarm limits: subagents, stuck dependents, stuck turns and the journaled reason of every stop."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import apierr
import config
import direct
import journal
from keepwarm_harness import START, drive, install, session as warm_session
from session import _Dependent
from test_admin import admin_relay  # noqa: F401 - the admin fixture
from test_keepwarm_account_state import unavailable_backend
from test_native_context_reuse import FIRST, request, run, setup

FINAL = [{"type": "text", "delta": "done"}, {"type": "end", "reason": "stop"}]
TOOL = [{"type": "tool_call", "id": "call-1", "name": "read_file", "arguments": '{"path": "a"}'},
        {"type": "end", "reason": "stop"}]


def stops(mark):
    return [dict(event["data"], level=event["level"]) for event in journal.JOURNAL.query(since=mark, limit=1000)
            if event["kind"] == "cache.keepwarm_stop"]


def dependent(child=None, since=START):
    record = _Dependent()
    record.since, record.session = since, child
    return record


# -- one conversation, simulated clock -----------------------------------------------------------
def test_idle_limit_counts_from_the_last_real_request_not_from_the_last_ping(monkeypatch):
    target = warm_session(_warm_at=START - 100)
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, target, seconds=5000)
    assert len(pings) == 4 and pings[-1] - START < 900
    assert 900 < clock.now - START < 903
    [stop] = stops(mark)
    assert (stop["reason"], stop["limit"], stop["subagent"]) == ("idle_limit", 900, False)
    assert stop["pings"] == stop["idle_pings"] == 4 and stop["credits"] == pytest.approx(0.4)
    assert 900 <= stop["idle_seconds"] <= 903
    assert stop["account"] == journal.tag("acc-keepwarm") and stop["level"] == "info"


def test_a_subagent_that_gave_its_final_answer_is_not_warmed_at_all(monkeypatch):
    target = warm_session(_parent_key="main", _turn_final=True, _warm_at=START - 100)
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, target, seconds=3000)
    assert pings == [] and clock.now - START < 2
    [stop] = stops(mark)
    assert (stop["reason"], stop["subagent"], stop["pings"], stop["credits"]) == ("subagent_done", True, 0, 0)


def test_a_subagent_that_asked_for_a_tool_is_warmed_only_for_the_subagent_limit(monkeypatch):
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, warm_session(_parent_key="main", _turn_final=False, _warm_at=START - 100),
                         seconds=5000)
    assert len(pings) == 2 and pings[-1] - START < 600
    assert 600 < clock.now - START < 603
    [stop] = stops(mark)
    assert (stop["reason"], stop["limit"], stop["subagent"], stop["pings"]) == ("idle_limit", 600, True, 2)
    # The same answer in a main conversation keeps warming up to the main limit.
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, warm_session(_turn_final=False, _warm_at=START - 100), seconds=5000)
    assert len(pings) == 4 and 900 < clock.now - START < 903
    assert [(s["reason"], s["limit"], s["subagent"]) for s in stops(mark)] == [("idle_limit", 900, False)]


def test_a_zero_subagent_limit_means_no_idle_warming_between_subagent_turns(monkeypatch):
    clock, pings = drive(monkeypatch, warm_session(_parent_key="main", _warm_at=START - 100),
                         {"cache_keepwarm_subagent_idle": 0}, seconds=5000)
    assert pings == [] and clock.now - START < 2


@pytest.mark.parametrize("case", ["counter_only", "quiet_child", "stuck_busy_child", "pending_child"])
def test_a_dependent_that_stopped_working_cannot_pin_its_parent_forever(monkeypatch, case):
    quiet = SimpleNamespace(_busy=False, _turn_started_at=0, _last_request_at=START)
    stuck = SimpleNamespace(_busy=True, _turn_started_at=START, _last_request_at=START)
    records = {"counter_only": set(), "quiet_child": {dependent(quiet)}, "stuck_busy_child": {dependent(stuck)},
               "pending_child": {dependent(None)}}[case]
    parent = warm_session(_last_request_at=START - 5000, _warm_at=START - 100, _cache_dependents=1,
                          _cache_dependent_records=records)
    mark = journal.JOURNAL.last_seq
    clock, _ = drive(monkeypatch, parent, {"cache_keepwarm_idle": 60}, seconds=5000)
    expected = {"counter_only": 0, "quiet_child": 600, "stuck_busy_child": 900, "pending_child": 900}[case]
    assert expected <= clock.now - START < expected + 3, case
    assert [s["reason"] for s in stops(mark)] == ["idle_limit"]


def test_a_working_dependent_keeps_its_parent_warm_beyond_the_parent_limit(monkeypatch):
    child = SimpleNamespace(_busy=True, _turn_started_at=START, _last_request_at=START)
    parent = warm_session(_last_request_at=START - 5000, _warm_at=START - 100, _cache_dependents=1,
                          _cache_dependent_records={dependent(child)})
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, parent, {"cache_keepwarm_idle": 60, "turn_timeout": 3600}, seconds=1500)
    assert clock.now - START >= 1500 and len(pings) >= 5
    assert [s["reason"] for s in stops(mark)] == ["closed"]  # only the end of the simulation stopped it


def test_a_turn_busy_past_turn_timeout_no_longer_extends_the_warm_up(monkeypatch):
    target = warm_session(_busy=True, _turn_started_at=START, _turn_seq=1, _turn_body={"model": "claude-opus-5-5"})
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, target, {"cache_keepwarm_idle": 1800, "turn_timeout": 900}, seconds=9000)
    assert 1800 < clock.now - START < 1803, "a turn that never ends must not warm forever"
    during = [e["data"] for e in journal.JOURNAL.query(since=mark, limit=1000)
              if e["kind"] == "cache.keepwarm" and e["data"]["during_turn"]]
    assert len(during) == 3 and all(item["turn_age_seconds"] <= 900 for item in during)
    [stop] = stops(mark)
    assert stop["reason"] == "idle_limit" and stop["idle_pings"] == len(pings) - 3


def test_pings_inside_a_subagent_turn_are_not_cut_by_the_subagent_limit(monkeypatch):
    target = warm_session(_parent_key="main", _busy=True, _turn_started_at=START, _turn_seq=1,
                          _turn_body={"model": "claude-opus-5-5"})
    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, target, seconds=850)  # past the 600 s subagent limit, inside turn_timeout
    assert [pytest.approx(t - START, abs=2) for t in pings] == [200, 440, 680]
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["idle_pings"]) == ("closed", 3, 0)


def test_five_failed_pings_stop_the_cycle_and_say_so(monkeypatch):
    def fail(*_args):
        raise OSError("synthetic failure")

    mark = journal.JOURNAL.last_seq
    clock, pings = drive(monkeypatch, warm_session(_warm_at=START - 100), {"cache_keepwarm_idle": 86400},
                         seconds=20000, ping=fail)
    assert len(pings) == 5 and clock.now - START < 20000
    [stop] = stops(mark)
    assert (stop["reason"], stop["failures"], stop["pings"], stop["level"]) == ("failures", 5, 5, "warn")


@pytest.mark.parametrize("state", ["disabled", "exhausted", "auth_dead"])
def test_an_unavailable_account_stop_names_its_state(monkeypatch, state):
    mark = journal.JOURNAL.last_seq
    drive(monkeypatch, warm_session(backend=unavailable_backend(state), _warm_at=START - 100), seconds=3000)
    [stop] = stops(mark)
    assert (stop["reason"], stop["account_state"], stop["pings"], stop["level"]) == (
        "account_unavailable", state, 0, "debug")


def test_a_session_closed_before_any_ping_leaves_no_stop_event_and_a_pinged_one_does(monkeypatch):
    mark = journal.JOURNAL.last_seq
    drive(monkeypatch, warm_session(_warm_at=START + 1000), seconds=3)
    assert stops(mark) == []
    drive(monkeypatch, warm_session(_warm_at=START - 100), seconds=200)
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["credits"]) == ("closed", 1, pytest.approx(0.1))


def test_the_stop_event_names_the_conversation_it_belongs_to(monkeypatch):
    identity = {"client": "opencode", "client_session_id": "child-1", "parent_session_id": "main-1"}
    mark = journal.JOURNAL.last_seq
    drive(monkeypatch, warm_session(_parent_key="main", _turn_final=True, _warm_identity=identity), seconds=60)
    [stop] = stops(mark)
    assert (stop["client_session_id"], stop["parent_session_id"]) == ("child-1", "main-1")
    assert stop["thread"] == journal.tag("thread") and stop["model"] == "claude-opus-5-5"


# -- the real pool and orchestrator --------------------------------------------------------------
def pooled(tmp_path, **settings):
    options = dict(transport="direct", project_context=False, cookie_values=[], cache_keepwarm_seconds=240,
                   cache_keepwarm_idle=900, cache_keepwarm_subagent_idle=600, conversation_idle_timeout=900,
                   turn_timeout=900, account_id="acc-limits")
    options.update(settings)
    return setup(tmp_path, **options)


def answers(monkeypatch, *script):
    """The direct transport plays one scripted answer per upstream request."""
    plan = list(script)

    async def stream(_cfg, _target, _body, _params):
        for event in plan.pop(0):
            yield event

    monkeypatch.setattr(direct, "_stream_body", stream)


def subagent():
    parsed = request(FIRST, key="header:child")
    parsed.parent_conversation_key = "header:main"
    return parsed


def test_a_real_subagent_turn_without_a_tool_call_ends_its_warm_up_at_once(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path)
    clock, pings = install(monkeypatch)
    answers(monkeypatch, FINAL)
    mark = journal.JOURNAL.last_seq

    async def scenario():
        holder = {}
        assert (await run(runner, subagent(), holder))[-1] == {"type": "end", "reason": "stop"}
        child = holder["session"].session
        await asyncio.wait_for(child._warm_task, 5)
        return child

    child = asyncio.run(scenario())
    assert pings == [] and child._turn_final is True
    [stop] = stops(mark)
    assert (stop["reason"], stop["subagent"], stop["pings"]) == ("subagent_done", True, 0)
    assert stop["account"] == journal.tag("acc-limits")


def test_a_real_subagent_turn_that_asked_for_a_tool_is_warmed_for_the_subagent_limit_only(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path)
    owners = []
    clock, pings = install(monkeypatch, ping=lambda owner, body: owners.append(owner))
    answers(monkeypatch, TOOL)
    mark = journal.JOURNAL.last_seq

    async def scenario():
        holder = {}
        clock.frozen = True  # the turn itself takes no simulated time
        assert (await run(runner, subagent(), holder))[-1] == {"type": "end", "reason": "tool_calls"}
        child = holder["session"].session
        clock.frozen = False
        await asyncio.wait_for(child._warm_task, 5)
        return child

    child = asyncio.run(scenario())
    assert child._turn_final is False and owners == [child, child]
    assert [round(t - START) for t in pings] == [240, 480]
    assert 600 < clock.now - START < 603
    [stop] = stops(mark)
    assert (stop["reason"], stop["limit"], stop["pings"], stop["subagent"]) == ("idle_limit", 600, 2, True)


def test_a_main_conversation_that_ended_in_final_text_is_still_warmed(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path)
    clock, pings = install(monkeypatch)
    answers(monkeypatch, FINAL)
    mark = journal.JOURNAL.last_seq

    async def scenario():
        holder = {}
        clock.frozen = True
        await run(runner, request(FIRST, key="header:main"), holder)
        main = holder["session"].session
        clock.frozen = False
        await asyncio.wait_for(main._warm_task, 5)

    asyncio.run(scenario())
    assert len(pings) == 3  # 240, 480, 720 s after the request; the 900 s limit ends it
    assert [(s["reason"], s["subagent"], s["limit"]) for s in stops(mark)] == [("idle_limit", False, 900)]


def test_a_child_lease_that_is_never_released_cannot_keep_its_parent_warm(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path, cache_keepwarm_idle=60)
    clock, pings = install(monkeypatch)
    answers(monkeypatch, FINAL)
    mark = journal.JOURNAL.last_seq

    async def scenario():
        holder = {}
        clock.frozen = True
        await run(runner, request(FIRST, key="header:main"), holder)
        parent = holder["session"].session
        lease = await pool.acquire("header:child", parent_key="header:main")  # never released: a lost release
        assert parent._cache_dependents == 1 and len(parent._cache_dependent_records) == 1
        clock.frozen = False
        await asyncio.wait_for(parent._warm_task, 5)
        stuck = parent._cache_dependents
        await pool.release(lease)
        return parent, stuck

    parent, stuck = asyncio.run(scenario())
    assert stuck == 1, "the lease count alone still says a child is running"
    assert 600 <= clock.now - START < 603, "...but the dependent's own limit ends the pin"
    assert [s["reason"] for s in stops(mark)] == ["idle_limit"]
    assert parent._cache_dependents == 0 and not parent._cache_dependent_records


def test_dependent_bookkeeping_is_balanced_and_the_count_never_goes_below_zero(tmp_path):
    _, pool, _ = pooled(tmp_path)

    async def scenario():
        main = await pool.acquire("header:main")
        await pool.release(main)
        parent = main.session
        child = await pool.acquire("header:child", parent_key="header:main")
        records = list(parent._cache_dependent_records)
        assert parent._cache_dependents == 1 and [r.session for r in records] == [child.session]
        await pool.release(child)
        await pool.release(child)  # a repeated release changes nothing
        assert parent._cache_dependents == 0 and not parent._cache_dependent_records
        for _ in range(3):
            pool._touch_cache_parents([("header:main", parent)], -1)
        assert parent._cache_dependents == 0
        pool._touch_cache_parents([("header:main", parent)], 1)
        pool._touch_cache_parents([("header:main", parent)], -1)
        pool._touch_cache_parents([("header:main", parent)], -1)
        assert parent._cache_dependents == 0

    asyncio.run(scenario())


def test_eviction_reports_why_the_warm_up_ended(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path)
    answers(monkeypatch, FINAL)
    mark = journal.JOURNAL.last_seq

    async def scenario():
        holder = {}
        await run(runner, request(FIRST, key="header:main"), holder)
        main = holder["session"].session
        task = main._warm_task
        assert task is not None and not task.done()
        await asyncio.sleep(0.2)  # the scheduler is really running, not cancelled before its first step
        pool._last_used[next(iter(pool._conversations))] = time.monotonic() - 10_000
        await pool._evict_idle()
        assert task.done() and not pool._conversations

    asyncio.run(scenario())
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["subagent"]) == ("evicted", 0, False)


# -- settings ------------------------------------------------------------------------------------
def write_config(tmp_path, **extra):
    saved = {"base": "https://example.invalid", "page_uuid": "page-1", "cookie": "accessToken=abc; xsrfToken=q",
             "xsrf": "q", "ua": "test", "workspace_root": str(tmp_path)}
    saved.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    return path


@pytest.fixture
def quiet_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RETOOL_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RELAY_API_KEY", "synthetic-key")


def test_keepwarm_limit_defaults_and_validation(tmp_path, quiet_environment):
    assert config.DEFAULTS["cache_keepwarm_idle"] == 900 == config.DEFAULTS["conversation_idle_timeout"]
    assert config.DEFAULTS["cache_keepwarm_subagent_idle"] == 600
    assert config.load(write_config(tmp_path))["cache_keepwarm_subagent_idle"] == 600
    assert config.load(write_config(tmp_path, cache_keepwarm_subagent_idle="300"))["cache_keepwarm_subagent_idle"] == 300
    assert config.load(write_config(tmp_path, cache_keepwarm_subagent_idle=0))["cache_keepwarm_subagent_idle"] == 0
    for bad in (-1, "soon", None):
        with pytest.raises(apierr.ApiError, match="cache_keepwarm_subagent_idle"):
            config.load(write_config(tmp_path, cache_keepwarm_subagent_idle=bad))


def test_admin_exposes_and_validates_the_subagent_limit(admin_relay):
    _, body, _ = admin_relay.request("/admin/api/settings")
    field = body["schema"]["cache_keepwarm_subagent_idle"]
    assert (field["type"], field["group"], field["unit"], field["min"], field["max"]) == ("integer", "cache", "с", 0, 86400)
    assert len(field["description"]) > 25 and body["settings"]["cache_keepwarm_subagent_idle"] == 600
    status, result, _ = admin_relay.patch({"cache_keepwarm_subagent_idle": 300})
    assert status == 200 and result["settings"]["cache_keepwarm_subagent_idle"] == 300
    assert "cache_keepwarm_subagent_idle" in result["pending_restart"]
    assert admin_relay.patch({"cache_keepwarm_subagent_idle": -1})[0] == 400
    assert admin_relay.patch({"cache_keepwarm_subagent_idle": 86401})[0] == 400
