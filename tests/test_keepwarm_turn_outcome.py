"""Only a turn that gave a usable answer becomes the keepwarm body; any other stops the cycle until a good one."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

import apierr
import client_context
import direct
import journal
from keepwarm_harness import START, drive, install, session as warm_session, stops
from test_native_context_reuse import FIRST, continued, request, run, setup

TEXT = [{"type": "text", "delta": "done"}, {"type": "end", "reason": "stop"}]
TOOL = [{"type": "tool_call", "id": "call-1", "name": "read_file", "arguments": '{"path": "a"}'},
        {"type": "end", "reason": "stop"}]
EMPTY = [{"type": "end", "reason": "stop"}]
THINKING = [{"type": "thinking_status", "source": "upstream"}, {"type": "thinking_start"},
            {"type": "thinking_signature", "signature": "opaque"}, {"type": "end", "reason": "stop"}]
BODY_A = {"model": "model-A", "openAICacheKey": "key-A"}
BODY_B = {"model": "model-B", "openAICacheKey": "key-B"}
IDENTITY_A = {"client": "opencode", "request_id": "req-A"}
IDENTITY_B = {"client": "opencode", "request_id": "req-B"}


# -- the verdict on one turn (a real iter_events over a scripted stream) -------------------------
def turn_session(**fields):
    """A session that has already had a good turn A: its body is warm, its touch is old (5.0)."""
    state = dict(_cancelled=False, thread_id="t", _warm_body=BODY_A, _warm_identity=IDENTITY_A, _warm_at=5.0,
                 _last_request_at=5.0)
    state.update(fields)
    return SimpleNamespace(**state)


def scripted(monkeypatch, script):
    """The upstream stream plays `script`; an exception in it is raised at that point. Returns the _ensure_keepwarm calls."""
    ensured = []

    async def stream(*_args):
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item

    monkeypatch.setattr(direct, "build_body", lambda *_args, **_kw: BODY_B)
    monkeypatch.setattr(direct, "_ensure_keepwarm", lambda *args: ensured.append(args))
    monkeypatch.setattr(direct, "_stream_body", stream)
    return ensured


def turn(session, *, stop_after=None):
    """Consume one turn as the orchestrator does; `stop_after(event)` makes the consumer walk away at that event."""
    async def consume():
        token = client_context.bind(IDENTITY_B)
        agen = direct.iter_events({}, session, {"model": "model-B"}, "", "", None, "auto", 32)
        try:
            async for event in agen:
                if stop_after is not None and stop_after(event):
                    break
        finally:
            client_context.reset(token)
            await agen.aclose()

    try:
        asyncio.run(consume())
    except apierr.ApiError as error:
        return error
    return None


VERDICTS = [
    ("text", TEXT, "content"),
    ("tool call", TOOL, "content"),
    ("text after thinking", THINKING[:-1] + TEXT, "content"),
    ("nothing at all", EMPTY, "empty"),
    ("blank text", [{"type": "text", "delta": " \n"}, {"type": "final_text", "text": " \n"}] + EMPTY, "empty"),
    ("a tool call that never finished", [{"type": "tool_call", "id": "c", "name": "read_file", "arguments": "",
                                          "partial": True}] + EMPTY, "empty"),
    ("thinking and a signature only", THINKING, "thinking_only"),
    ("a heartbeat only", [{"type": "thinking_heartbeat", "elapsed": 30.0}] + EMPTY, "thinking_only"),
    ("text, then the stream was cut", [{"type": "text", "delta": "half"},
                                       {"type": "error", "error": apierr.upstream_error("cut")}], "truncated"),
    ("cancelled", [{"type": "end", "reason": "cancelled"}], "cancelled"),
    ("a server error", [apierr.upstream_error("boom")], "error"),
    ("a timeout after some text", [{"type": "text", "delta": "partial"}, apierr.upstream_timeout("slow")], "error"),
    ("an hourly limit", [apierr.too_many_requests("limit")], "error"),
    ("an auth failure", [apierr.upstream_auth_failed("expired")], "error"),
    ("a rejected body (400)", [apierr.bad_request("poison")], "rejected"),
    ("a rejected body (413)", [apierr.payload_too_large(100)], "rejected"),
    ("a rejected body (422)", [apierr.unprocessable("schema")], "rejected"),
    ("an unexpected crash", [RuntimeError("bug")], "error"),
]


@pytest.mark.parametrize("script,outcome", [item[1:] for item in VERDICTS], ids=[item[0] for item in VERDICTS])
def test_only_a_usable_answer_becomes_the_keepwarm_body(monkeypatch, script, outcome):
    ensured = scripted(monkeypatch, script)
    session = turn_session()
    try:
        turn(session)
    except RuntimeError:
        pass
    assert session._turn_outcome == outcome
    assert session._busy is False and session._turn_body is None and session._turn_identity is None
    if outcome == "content":
        assert session._warm_body is BODY_B and session._warm_identity == IDENTITY_B
        assert session._warm_at == session._turn_started_at > 5.0
        assert len(ensured) == 2, "the scheduler is started with the turn and again at its end"
    else:
        # The good turn A stays the body: the failed one neither replaces it nor moves its touch.
        assert session._warm_body is BODY_A and session._warm_identity == IDENTITY_A and session._warm_at == 5.0
        assert len(ensured) == 1, "nothing is started at the end of a turn that gave no answer"
    # A rejected body never reached the cache; every other turn did, and its key is the one last sent.
    assert getattr(session, "_last_cache_key", None) == (None if outcome == "rejected" else "key-B")


def test_the_verdict_table_covers_every_outcome_of_the_attempt_trace():
    named = {outcome for _name, _script, outcome in VERDICTS}
    assert {"content", "empty", "thinking_only", "error", "truncated", "cancelled", "rejected"} <= named


def test_a_turn_without_a_cache_key_is_not_judged_at_all(monkeypatch):
    ensured = scripted(monkeypatch, EMPTY)
    monkeypatch.setattr(direct, "build_body", lambda *_args, **_kw: {"model": "model-B"})
    session = turn_session(_turn_outcome="content")
    turn(session)
    assert session._turn_outcome == "content" and session._warm_body is BODY_A and ensured == []


def test_a_cancelled_client_never_makes_the_turn_a_warm_body(monkeypatch):
    """The session was cancelled while a complete answer was still arriving: the client is gone."""
    scripted(monkeypatch, TEXT)
    session = turn_session(_cancelled=True)
    turn(session)
    assert session._turn_outcome == "cancelled" and session._warm_body is BODY_A


def test_a_consumer_that_walks_away_mid_answer_is_a_cancel(monkeypatch):
    scripted(monkeypatch, [{"type": "text", "delta": "start"}] + TEXT)
    session = turn_session()
    turn(session, stop_after=lambda event: event["type"] == "text")
    assert session._turn_outcome == "cancelled" and session._warm_body is BODY_A


def test_a_consumer_that_stops_at_the_error_of_a_cut_stream_still_sees_a_cut_stream(monkeypatch):
    """The orchestrator raises on that event and the generator is closed afterwards, which is no cancel."""
    scripted(monkeypatch, [{"type": "text", "delta": "half"},
                           {"type": "error", "error": apierr.upstream_error("cut")}])
    session = turn_session()
    turn(session, stop_after=lambda event: event["type"] == "error")
    assert session._turn_outcome == "truncated" and session._warm_body is BODY_A


def test_a_good_turn_after_a_failed_one_is_a_warm_body_again(monkeypatch):
    session = turn_session()
    scripted(monkeypatch, THINKING)
    turn(session)
    assert session._turn_outcome == "thinking_only"
    ensured = scripted(monkeypatch, TEXT)
    turn(session)
    assert session._turn_outcome == "content" and session._warm_body is BODY_B and len(ensured) == 2


def test_the_verdict_uses_the_attempt_classification_of_the_trace():
    classify = direct.attempt_outcome
    assert classify(cancelled=True, failed=True, ended=False, usable=True, thinking_seen=True) == "cancelled"
    assert classify(cancelled=False, failed=True, ended=True, usable=True, thinking_seen=False) == "error"
    assert classify(cancelled=False, failed=False, ended=False, usable=True, thinking_seen=False) == "truncated"
    assert classify(cancelled=False, failed=False, ended=True, usable=True, thinking_seen=True) == "content"
    assert classify(cancelled=False, failed=False, ended=True, usable=False, thinking_seen=True) == "thinking_only"
    assert classify(cancelled=False, failed=False, ended=True, usable=False, thinking_seen=False) == "empty"


# -- the cycle after a verdict -------------------------------------------------------------------
@pytest.mark.parametrize("outcome", ["empty", "thinking_only", "truncated", "error", "rejected", "cancelled"])
def test_a_cycle_that_meets_a_failed_turn_stops_at_once_and_says_why(monkeypatch, outcome):
    mark = journal.JOURNAL.last_seq
    target = warm_session(_turn_outcome=outcome, _warm_at=START - 100)
    clock, pings = drive(monkeypatch, target, seconds=3000)
    assert pings == [] and clock.now - START < 2
    [stop] = stops(mark)
    assert (stop["reason"], stop["turn_outcome"], stop["pings"], stop["credits"], stop["level"]) == (
        "last_turn_failed", outcome, 0, 0, "info")
    assert direct._STOP_TEXT["last_turn_failed"] in [
        e["message"] for e in journal.JOURNAL.query(since=mark, limit=100) if e["kind"] == "cache.keepwarm_stop"]


def test_a_turn_that_fails_while_the_cycle_runs_stops_it_at_the_next_look(monkeypatch):
    mark = journal.JOURNAL.last_seq
    target = warm_session(_turn_outcome="content", _warm_at=START - 100)

    def fail_the_turn(now):
        if now > START + 300 and target._turn_outcome == "content":
            target._turn_outcome = "empty"

    clock, pings = drive(monkeypatch, target, seconds=3000, on_tick=fail_the_turn)
    assert len(pings) == 1, "the ping due at 380 s never goes out"
    assert START + 300 < clock.now < START + 302
    [stop] = stops(mark)
    assert (stop["reason"], stop["turn_outcome"], stop["pings"]) == ("last_turn_failed", "empty", 1)


def test_a_failed_turn_does_not_stop_the_pings_inside_the_next_turn(monkeypatch):
    """The outcome describes the turn that ended. A turn in flight is judged when it ends, not before."""
    target = warm_session(_turn_outcome="empty", _busy=True, _turn_started_at=START, _turn_seq=1,
                          _turn_body={"model": "claude-opus-5-5"}, _warm_at=0)
    _, pings = drive(monkeypatch, target, {"turn_timeout": 3000}, seconds=300)
    assert [round(t - START) for t in pings] == [200]


@pytest.mark.parametrize("outcome,age,busy,starts", [
    (None, 100, False, True),
    ("content", 100, False, True),
    ("empty", 100, False, False),
    ("thinking_only", 100, False, False),
    ("error", 100, False, False),
    ("empty", 400, True, True),
    ("content", 400, True, True),
])
def test_a_cycle_is_started_between_turns_only_for_a_conversation_whose_last_turn_was_good(
        monkeypatch, outcome, age, busy, starts):
    """Parents pinned by a child get their cycle restarted at every lease: a failed one must not stop and say so each time."""
    monkeypatch.setattr(direct, "_ping", lambda *_args: pytest.fail("no ping is due within this test"))

    async def scenario():
        target = SimpleNamespace(_cancelled=False, _busy=busy, _turn_started_at=time.time() - 1 if busy else 0,
                                 _turn_seq=1, _turn_body=None, _warm_body=dict(BODY_A), _warm_at=time.time() - age,
                                 _last_request_at=time.time(), _warm_failures=0, _keepwarm_cost=0.0, thread_id="t")
        if outcome is not None:
            target._turn_outcome = outcome
        direct._ensure_keepwarm(target, {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 900})
        task = getattr(target, "_warm_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return task is not None

    assert asyncio.run(scenario()) is starts


# -- the real pool and orchestrator --------------------------------------------------------------
def pooled(tmp_path, **settings):
    options = dict(transport="direct", project_context=False, cookie_values=[], cache_keepwarm_seconds=240,
                   cache_keepwarm_idle=900, cache_keepwarm_subagent_idle=600, conversation_idle_timeout=900,
                   turn_timeout=900, account_id="acc-outcome")
    options.update(settings)
    return setup(tmp_path, **options)


def answers(monkeypatch, *script):
    """The direct transport plays one scripted answer per upstream request; returns the bodies it was sent."""
    plan, sent = list(script), []

    async def stream(_cfg, _target, body, _params):
        sent.append(copy.deepcopy(body))
        for event in plan.pop(0):
            yield event

    monkeypatch.setattr(direct, "_stream_body", stream)
    return sent


def test_a_thinking_only_answer_is_neither_a_warm_body_nor_a_reason_to_ping(tmp_path, monkeypatch):
    """The live case: upstream returned only thinking and a signature, and the relay went on warming that request."""
    _, pool, runner = pooled(tmp_path)
    clock, pings = install(monkeypatch)
    answers(monkeypatch, THINKING)
    mark = journal.JOURNAL.last_seq

    async def scenario():
        holder = {}
        clock.frozen = True  # the turn itself takes no simulated time
        events = await run(runner, request(FIRST, key="header:main"), holder)
        main = holder["session"].session
        clock.frozen = False
        await asyncio.wait_for(main._warm_task, 5)
        return events, main

    events, main = asyncio.run(scenario())
    assert events[-1]["type"] == "error" and events[-1]["error"].code == "upstream_empty_turn"
    assert pings == [] and main._warm_body is None and main._turn_outcome == "thinking_only"
    assert clock.now - START < 2, "no ping is waiting in the 15 minutes after it"
    [stop] = stops(mark)
    assert (stop["reason"], stop["turn_outcome"], stop["pings"], stop["credits"]) == (
        "last_turn_failed", "thinking_only", 0, 0)
    assert stop["account"] == journal.tag("acc-outcome")


def test_a_failed_turn_keeps_the_last_good_body_and_a_good_turn_after_it_warms_again(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path)
    clock, pings = install(monkeypatch)
    sent = answers(monkeypatch, TEXT, THINKING, TEXT)
    mark = journal.JOURNAL.last_seq
    first, second = request(FIRST, key="header:main"), request(continued(FIRST), key="header:main")

    async def scenario():
        clock.frozen = True
        holder = {}
        assert (await run(runner, first, holder))[-1] == {"type": "end", "reason": "stop"}
        main = holder["session"].session
        good_body = copy.deepcopy(main._warm_body)
        first_cycle = main._warm_task
        assert (await run(runner, second, {}))[-1]["type"] == "error"
        assert main._warm_body == good_body and main._turn_outcome == "thinking_only"
        assert len(good_body["messages"]) < len(sent[1]["messages"]), "the failed turn was a longer request"
        clock.frozen = False
        await asyncio.wait_for(first_cycle, 5)  # the failed turn ended the cycle the good one had started
        assert pings == [] and [s["reason"] for s in stops(mark)] == ["last_turn_failed"]
        clock.frozen = True
        assert (await run(runner, second, {}))[-1] == {"type": "end", "reason": "stop"}
        assert main._turn_outcome == "content" and main._warm_body == sent[2]
        assert main._warm_task is not first_cycle and not main._warm_task.done()
        clock.frozen = False
        await asyncio.wait_for(main._warm_task, 5)

    asyncio.run(scenario())
    assert [s["reason"] for s in stops(mark)] == ["last_turn_failed", "idle_limit"]
    assert len(pings) == 3, "240, 480 and 720 s after the good turn; nothing for the failed one"


def test_a_turn_that_gave_a_tool_call_is_warmed_like_one_that_gave_text(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path)
    clock, pings = install(monkeypatch)
    answers(monkeypatch, TOOL)
    mark = journal.JOURNAL.last_seq

    async def scenario():
        holder = {}
        clock.frozen = True
        assert (await run(runner, request(FIRST, key="header:main"), holder))[-1] == {"type": "end", "reason": "tool_calls"}
        main = holder["session"].session
        clock.frozen = False
        await asyncio.wait_for(main._warm_task, 5)
        return main

    main = asyncio.run(scenario())
    assert main._turn_outcome == "content" and len(pings) == 3
    assert [s["reason"] for s in stops(mark)] == ["idle_limit"]


def test_an_upstream_error_turn_is_not_warmed_either(tmp_path, monkeypatch):
    _, pool, runner = pooled(tmp_path)
    clock, pings = install(monkeypatch)
    mark = journal.JOURNAL.last_seq

    async def stream(_cfg, _target, _body, _params):
        yield {"type": "text", "delta": "partial"}
        raise apierr.upstream_error("connection reset")

    monkeypatch.setattr(direct, "_stream_body", stream)

    async def scenario():
        holder = {}
        clock.frozen = True
        events = await run(runner, request(FIRST, key="header:main"), holder)
        main = holder["session"].session
        clock.frozen = False
        await asyncio.wait_for(main._warm_task, 5)
        return events, main

    events, main = asyncio.run(scenario())
    assert events[-1]["type"] == "error" and main._turn_outcome == "error" and main._warm_body is None
    assert pings == [] and [(s["reason"], s["turn_outcome"]) for s in stops(mark)] == [("last_turn_failed", "error")]
