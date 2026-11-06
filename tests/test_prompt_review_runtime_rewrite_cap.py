"""R4-04: the cap on what a session pays for rewrites belongs to the session, not to one cycle: the pool asks for a new cycle
of a parent at every request of its child, and a cycle that remembers nothing pays for one more rewrite each time."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import apierr
import client_context
import direct
import journal
from keepwarm_harness import CFG, START, install, session as warm_session, stops
from test_keepwarm_turn_outcome import BODY_A, BODY_B, IDENTITY_A, TEXT
from test_native_context_reuse import FIRST, request, run, setup

REWRITE = {"promptTokensWrittenToCache": 190_000, "promptTokensReadFromCache": 0}
REAL_ENSURE = direct._ensure_keepwarm  # what the pool calls, before any test replaces it


def rewriting_ping(owner, body):
    owner._last_warm_usage = dict(REWRITE)
    return 19.0


# -- the cycle that found its prefix gone ---------------------------------------------------------------------------------
def test_a_parent_whose_cycle_stopped_for_a_rewrite_is_not_restarted_at_the_requests_of_its_child(monkeypatch):
    mark = journal.JOURNAL.last_seq
    clock, pings = install(monkeypatch, ping=rewriting_ping)
    target = warm_session(_warm_at=START - 100, _cancelled=False, _cache_dependents=1, _warm_task=None)

    async def story():
        REAL_ENSURE(target, dict(CFG))
        await asyncio.wait_for(target._warm_task, 5)
        first = len(pings)
        for _ in range(3):  # a subagent of this conversation leases a slot and releases it
            REAL_ENSURE(target, dict(CFG))
            await asyncio.wait_for(target._warm_task, 5)
        return first

    assert asyncio.run(story()) == 1
    assert len(pings) == 1 and round(target._keepwarm_cost, 1) == 19.0  # one rewrite, however many children ask again
    assert [(stop["reason"], stop["pings"]) for stop in stops(mark)] == [("cache_rewritten", 1)]
    assert target._warm_blocked == "cache_rewritten"


def test_the_real_pool_does_not_restart_it_either(tmp_path, monkeypatch):
    backend, pool, runner = setup(tmp_path, transport="direct", cache_keepwarm_seconds=240, cache_keepwarm_idle=900,
                                  cache_keepwarm_subagent_idle=600, conversation_idle_timeout=900, turn_timeout=900,
                                  base="https://fixture.invalid", cookie_values=[], project_context=False)
    backend.headers = lambda: {}
    clock, pings = install(monkeypatch, ping=rewriting_ping)
    mark = journal.JOURNAL.last_seq

    async def stream(_cfg, _target, body, _params):
        yield {"type": "text", "delta": "done"}
        yield {"type": "end", "reason": "stop"}

    monkeypatch.setattr(direct, "_stream_body", stream)

    async def story():
        clock.frozen = True
        await run(runner, request(FIRST, key="header:parent"))
        parent = pool._conversations["header:parent"]
        clock.frozen = False
        await asyncio.wait_for(parent._warm_task, 5)  # the cycle ends with its first, rewriting, ping
        clock.frozen = True
        for _ in range(3):  # a subagent of the parent makes three requests
            lease = await pool.acquire("header:child", model_entry=request(FIRST).entry, parent_key="header:parent")
            await pool.release(lease)
            clock.frozen = False
            await asyncio.wait_for(parent._warm_task, 5)
            clock.frozen = True
        return parent

    parent = asyncio.run(story())
    assert len(pings) == 1 and round(parent._keepwarm_cost, 1) == 19.0
    assert [(stop["reason"], stop["pings"]) for stop in stops(mark)] == [("cache_rewritten", 1)]


# -- what lifts it, and what it holds through ----------------------------------------------------------------------------------
def run_turn(session, identity, script, monkeypatch):
    """One real turn through iter_events on a scripted stream; _ensure_keepwarm stays the real one."""
    async def stream(*_args):
        for event in script:
            yield event

    monkeypatch.setattr(direct, "build_body", lambda *_a, **_k: BODY_B)
    monkeypatch.setattr(direct, "_stream_body", stream)

    async def consume():
        token = client_context.bind(identity)
        agen = direct.iter_events({}, session, {"model": "model-B"}, "", "", None, "auto", 32)
        try:
            async for _event in agen:
                pass
        finally:
            client_context.reset(token)
            await agen.aclose()
            task = getattr(session, "_warm_task", None)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(consume())


def marked_session(**fields):
    state = dict(_cancelled=False, thread_id="t", _warm_body=BODY_A, _warm_identity=IDENTITY_A, _warm_at=time.time(),
                 _last_request_at=time.time(), _warm_failures=0, _keepwarm_cost=0.0, _warm_blocked="cache_rewritten")
    state.update(fields)
    return SimpleNamespace(**state)


def test_a_real_turn_that_gives_a_usable_answer_lifts_the_cap_and_starts_a_cycle(monkeypatch):
    install(monkeypatch)
    session = marked_session()
    run_turn(session, {"client": "opencode", "request_id": "real"}, TEXT, monkeypatch)
    assert session._warm_blocked is None and session._turn_outcome == "content"
    assert session._warm_task is not None  # the cycle of the turn that ended well


@pytest.mark.parametrize("script,outcome", [
    ([{"type": "text", "delta": "half"}, {"type": "error", "error": apierr.upstream_error("cut")}], "truncated"),
    ([{"type": "end", "reason": "stop"}], "empty")])
def test_a_turn_that_gives_none_leaves_the_cap_where_it_was_and_starts_no_cycle(monkeypatch, script, outcome):
    install(monkeypatch)
    session = marked_session()
    run_turn(session, {"client": "opencode", "request_id": "real"}, script, monkeypatch)
    assert session._turn_outcome == outcome and session._warm_blocked == "cache_rewritten"
    assert getattr(session, "_warm_task", None) is None


def test_a_compaction_does_not_lift_the_cap_nor_replace_it_with_a_weaker_mark(monkeypatch):
    install(monkeypatch)
    session = marked_session()
    run_turn(session, {"client": "opencode", "client_task": client_context.COMPACTION}, TEXT, monkeypatch)
    assert session._turn_outcome == "content" and session._warm_blocked == "cache_rewritten"
    assert getattr(session, "_warm_task", None) is None


@pytest.mark.parametrize("fields,busy,starts", [
    ({"_warm_blocked": "cache_rewritten"}, False, False),
    ({"_warm_blocked": "cache_rewritten"}, True, False),      # the cap holds through a turn in flight too
    ({"_warm_blocked": "compacted"}, False, False),
    ({"_warm_blocked": "compacted"}, True, True),             # a turn in flight is judged when it ends
    ({"_turn_outcome": "error"}, False, False),
    ({"_turn_outcome": "error"}, True, True),
    ({"_turn_outcome": "error", "_warm_blocked": "compacted"}, False, False),
    ({}, False, True),
])
def test_which_marks_refuse_a_new_cycle_between_turns_and_which_in_a_turn(fields, busy, starts):
    async def scenario():
        target = marked_session(_warm_blocked=None, _busy=busy, _turn_started_at=time.time() - 1 if busy else 0,
                                _turn_seq=1, _turn_body=None)
        for name, value in fields.items():
            setattr(target, name, value)
        REAL_ENSURE(target, {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 900})
        task = getattr(target, "_warm_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return task is not None

    assert asyncio.run(scenario()) is starts


def test_a_failed_turn_and_a_compaction_after_it_still_refuse_the_cycle_until_a_real_success(monkeypatch):
    install(monkeypatch)
    session = marked_session(_warm_blocked=None)
    run_turn(session, {"client": "opencode", "request_id": "real"}, [{"type": "end", "reason": "stop"}], monkeypatch)
    assert session._turn_outcome == "empty"
    run_turn(session, {"client": "opencode", "client_task": client_context.COMPACTION}, TEXT, monkeypatch)
    # the compaction's own verdict is `content`, which must not make the dialog look healthy again
    assert session._turn_outcome == "content" and session._warm_blocked == "compacted"
    assert direct._warm_refused(session, False) == ("compacted", {})
    run_turn(session, {"client": "opencode", "request_id": "next"}, TEXT, monkeypatch)
    assert session._warm_blocked is None and direct._warm_refused(session, False) is None
