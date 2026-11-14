"""R4-02: the verdict of a cut stream lands when the stream says it is cut, not when the garbage collector frees the
generators; a generator that is closed late leaves the state of a newer turn alone."""
import asyncio
import gc

import pytest

import apierr
import client_context
import direct
import journal
from keepwarm_harness import install, stops
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from session import RelaySession
from test_conversation_state import DONE, processes  # noqa: F401 - processes is a fixture
from test_keepwarm_turn_outcome import BODY_A, BODY_B, IDENTITY_A, IDENTITY_B, turn_session
from test_native_context_reuse import FIRST, run, setup
from test_native_history import parsed
from test_review_fixes_pr19 import text

CUT = [{"type": "text", "delta": "half"}, {"type": "error", "error": apierr.upstream_error("cut")}]
GOOD = [{"type": "text", "delta": "done"}, {"type": "end", "reason": "stop"}]
OPENCODE = {"user-agent": "opencode/latest/2.2.5/desktop", "x-proxi-client": "opencode", "x-session-affinity": "ses_cut"}
BODY_C = {"model": "model-C", "openAICacheKey": "key-C"}


def streams(monkeypatch, *plays):
    """The transport plays one script per upstream request and builds the bodies given (in order)."""
    scripts, bodies = [item[0] for item in plays], [item[1] for item in plays]

    async def stream(*_args):
        for event in scripts.pop(0):
            yield event

    monkeypatch.setattr(direct, "build_body", lambda *_a, **_k: bodies.pop(0))
    monkeypatch.setattr(direct, "_ensure_keepwarm", lambda *_a: None)
    monkeypatch.setattr(direct, "_stream_body", stream)


def open_turn(session, identity=IDENTITY_B):
    return direct.iter_events({}, session, {"model": "model-B"}, "", "", None, "auto", 32), identity


async def next_event(agen, identity):
    token = client_context.bind(identity)
    try:
        return await agen.__anext__()
    finally:
        client_context.reset(token)


def state(session):
    return (session._busy, getattr(session, "_turn_outcome", None), session._turn_body, session._turn_identity)


# -- the verdict is passed at the error event -------------------------------------------------------------------------------
def test_the_verdict_of_a_cut_stream_is_recorded_before_the_error_reaches_the_consumer(monkeypatch):
    streams(monkeypatch, (CUT, BODY_B))
    session = turn_session()
    seen = []

    async def consume():
        agen, identity = open_turn(session)
        while True:
            event = await next_event(agen, identity)
            if event["type"] == "error":
                seen.append(state(session))
                break  # the consumer raises here, as the orchestrator does, and never resumes the generator
        await asyncio.sleep(0)
        seen.append(state(session))
        await agen.aclose()
        seen.append(state(session))

    asyncio.run(consume())
    assert seen == [(False, "truncated", None, None)] * 3
    assert session._warm_body is BODY_A and session._warm_at == 5.0  # a cut turn is no warm body


def test_a_cut_stream_is_settled_without_the_garbage_collector_ever_running(monkeypatch):
    streams(monkeypatch, (CUT, BODY_B))
    session = turn_session()
    held = []

    async def consume():
        agen, identity = open_turn(session)
        held.append(agen)  # a reference nobody drops: only the collector could ever close it
        while (await next_event(agen, identity))["type"] != "error":
            pass
        return state(session)

    gc.disable()
    try:
        assert asyncio.run(consume()) == (False, "truncated", None, None)
    finally:
        gc.enable()


# -- a late close is bound to its own turn ----------------------------------------------------------------------------------
def test_a_generator_closed_late_leaves_the_state_of_the_newer_turn_alone(monkeypatch):
    """Turn A is abandoned in flight (no verdict yet), turn B starts on the same session; closing A now must not mark
    the session idle, drop B's body or write a verdict over B's."""
    streams(monkeypatch, (GOOD[:1] + GOOD, BODY_C), (GOOD, BODY_B))
    session = turn_session()
    result = {}

    async def story():
        old, old_identity = open_turn(session, IDENTITY_A)
        assert (await next_event(old, old_identity))["type"] == "text"
        seq_old = session._turn_seq
        new, new_identity = open_turn(session)
        assert (await next_event(new, new_identity))["type"] == "text"
        assert session._turn_seq == seq_old + 1
        result["in_flight"] = state(session)
        await old.aclose()  # the collector gets to turn A while B is running
        result["after_late_close"] = state(session)
        async for _event in new:
            pass
        result["after_b"] = state(session)
        result["warm"] = (session._warm_body, session._warm_identity)

    asyncio.run(story())
    assert result["in_flight"] == (True, None, BODY_B, IDENTITY_B)
    assert result["after_late_close"] == result["in_flight"]
    assert result["after_b"] == (False, "content", None, None) and result["warm"] == (BODY_B, IDENTITY_B)


def test_a_cut_turn_closed_late_does_not_turn_a_later_good_turn_into_a_failure(monkeypatch):
    streams(monkeypatch, (CUT, BODY_C), (GOOD, BODY_B))
    session = turn_session()
    result = {}

    async def story():
        zombie, zombie_identity = open_turn(session, IDENTITY_A)
        while (await next_event(zombie, zombie_identity))["type"] != "error":
            pass
        assert session._turn_outcome == "truncated"
        good, good_identity = open_turn(session)
        assert (await next_event(good, good_identity))["type"] == "text"
        async for _event in good:
            pass
        result["before"] = state(session)
        await zombie.aclose()  # the late close of the cut turn
        result["after"] = state(session)

    asyncio.run(story())
    assert result["before"] == result["after"] == (False, "content", None, None)


def test_settling_twice_records_one_verdict(monkeypatch):
    streams(monkeypatch, (CUT, BODY_B))
    session = turn_session()
    notes = []
    real = journal.emit
    monkeypatch.setattr(journal, "emit", lambda kind, *a, **k: notes.append(kind) or real(kind, *a, **k))

    async def consume():
        agen, identity = open_turn(session)
        while (await next_event(agen, identity))["type"] != "error":
            pass
        await agen.aclose()

    asyncio.run(consume())
    assert notes.count("cache.key_changed") <= 1 and session._turn_outcome == "truncated"


# -- the generators of the stream are closed where the consumer leaves them -----------------------------------------------------------------
def test_the_runner_closes_the_stream_it_raises_on(tmp_path, monkeypatch):
    closed = []

    async def stream(session, prompt, provider, tool_names=None):
        try:
            yield {"type": "text", "delta": "half"}
            yield {"type": "error", "error": apierr.upstream_error("cut")}
            yield {"type": "text", "delta": "never read"}
        finally:
            closed.append("stream")

    monkeypatch.setattr(RelaySession, "stream", stream)
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[], exec_policy="off")

    async def story():
        events = await run(runner, parsed(FIRST))
        return events, list(closed)  # looked at before the loop ends: nothing is left to the loop's own cleanup

    gc.disable()
    try:
        events, closed_in_time = asyncio.run(story())
    finally:
        gc.enable()
    assert events[-1]["type"] == "error" and closed_in_time == ["stream"]


def test_the_lease_and_the_session_close_what_they_wrap(tmp_path, monkeypatch):
    closed = []

    async def iter_events(cfg, session, provider, prompt, head, tools, tool_choice, max_tokens, chunks=None):
        try:
            yield {"type": "text", "delta": "a"}
            yield {"type": "text", "delta": "b"}
        finally:
            closed.append("iter_events")

    monkeypatch.setattr(direct, "iter_events", iter_events)
    backend, pool, _ = setup(tmp_path, transport="direct", project_context=False, cookie_values=[])

    async def story():
        lease = await pool.acquire("header:closing", model_entry=parsed(FIRST).entry)
        agen = lease.stream("prompt", {"model": "claude-opus-5-5"})
        assert (await agen.__anext__())["type"] == "text"
        await agen.aclose()
        in_time = list(closed)
        await pool.release(lease)
        return in_time

    gc.disable()
    try:
        assert asyncio.run(story()) == ["iter_events"]
    finally:
        gc.enable()


# -- over HTTP, and with the cycle of the dialog -------------------------------------------------------------------------------------
def test_a_cut_stream_over_http_leaves_the_session_settled_when_the_response_is_out(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    relay.scripts.append([text("half an answer")])  # no end_of_stream: the transport yields an error event
    request = client_request("chat", [{"role": "user", "content": "task"}], model="claude-sonnet-5-5",
                             reasoning_effort="max")
    gc.disable()
    try:
        status, _, _ = relay.post("chat", request, OPENCODE)
        (session,) = relay.runtime.pool._conversations.values()
        assert status == 502
        assert (session._busy, session._turn_outcome) == (False, "truncated")
        assert session._turn_body is None and session._turn_identity is None
    finally:
        gc.enable()


def test_no_ping_is_paid_for_a_cut_turn(tmp_path, monkeypatch, processes):
    process = processes(tmp_path / "state", scripts=[[text("half an answer")], DONE], backend_options={
        "cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 900, "cache_keepwarm_subagent_idle": 600,
        "conversation_idle_timeout": 900, "turn_timeout": 900})
    process.runner.cfg.update(process.backends[0].cfg)
    clock, pings = install(monkeypatch)
    mark = journal.JOURNAL.last_seq
    gc.disable()
    try:
        async def story():
            clock.frozen = True
            await process.turn(parsed(FIRST))
            session = next(iter(process.pool._conversations.values()))
            clock.frozen = False
            await asyncio.wait_for(session._warm_task, 10)

        asyncio.run(story())
    finally:
        gc.enable()
    assert pings == []
    [stop] = stops(mark)
    assert (stop["reason"], stop["turn_outcome"], stop["pings"]) == ("last_turn_failed", "truncated", 0)
