"""A client's compaction is not kept warm while it runs: neither its own body nor the chat's last one is pinged (lens-credits-4).

The client replaces its history by the summary, so after a compaction nobody continues the body of the compaction request nor
the chat's last body (settle marks the session `compacted`). But the compaction armed the during-turn keepwarm with its own body
like any turn (only a background job was left out): a summary that took longer than cache_keepwarm_turn_offset was pinged at
200 s and every 240 s after, each ping reading the whole chat prefix (about 1.2 credits at 300k tokens on Opus) for a prefix
nobody reads again. Without its body the busy cycle would have pinged the chat's last body instead. Ordinary turns keep their
during-turn pings as before."""
import asyncio

import client_context
import direct
import journal
from keepwarm_harness import CFG, START, install, session as warm_session, stops
from test_keepwarm_turn_outcome import TEXT

CHAT_BODY = {"model": "claude-opus-5-5", "openAICacheKey": "key-chat", "messages": ["chat"]}
SUMMARY_BODY = {"model": "claude-opus-5-5", "openAICacheKey": "key-chat", "messages": ["chat", "summarize"]}
TURN_BODY = {"model": "claude-opus-5-5", "openAICacheKey": "key-chat", "messages": ["chat", "go on"]}
COMPACTION = {"client": "claude-code", "request_id": "req-compact", "client_task": client_context.COMPACTION}
CHAT = {"client": "claude-code", "request_id": "req-chat"}


def scripted(monkeypatch, body, clock=None, *, lasts=0.0, seen=None):
    """The upstream answers TEXT after `lasts` seconds of the simulated clock; `seen` gets the session's state mid-turn."""
    async def stream(_cfg, session, _body, *_args):
        if seen is not None:
            seen.append({"turn_body": session._turn_body, "busy": session._busy,
                         "compaction": getattr(session, "_turn_compaction", False)})
        if clock is not None:
            while clock.now < START + lasts:
                await asyncio.sleep(1.0)
        for event in TEXT:
            yield event

    monkeypatch.setattr(direct, "build_body", lambda *_args, **_kw: dict(body))
    monkeypatch.setattr(direct, "_stream_body", stream)


async def one_turn(session, identity):
    token = client_context.bind(identity)
    agen = direct.iter_events(dict(CFG), session, {"model": "claude-opus-5-5"}, "", "", None, "auto", 32)
    try:
        async for _event in agen:
            pass
    finally:
        client_context.reset(token)
        await agen.aclose()


def chat_session(**fields):
    """A chat whose last turn answered at START: its body is warm and its idle cycle is due at START + 240."""
    return warm_session(_cancelled=False, _warm_body=dict(CHAT_BODY), _warm_identity=dict(CHAT), _warm_at=START,
                        _last_request_at=START, _turn_outcome="content", _warm_task=None, **fields)


def story(monkeypatch, identity, body, *, lasts=460.0):
    """The chat's idle cycle runs; then a turn under `identity` takes `lasts` seconds. Returns the pinged bodies, their
    offsets from START, the session and the turn's state as the upstream saw it."""
    sent, seen = [], []
    clock, pings = install(monkeypatch, ping=lambda owner, pinged: sent.append(pinged) or 0.1)
    scripted(monkeypatch, body, clock, lasts=lasts, seen=seen)
    target = chat_session()

    async def run():
        direct._ensure_keepwarm(target, dict(CFG))  # what the chat's last turn left running
        cycle = target._warm_task
        await one_turn(target, identity)
        # The turn is over: a compacted dialog stops at the cycle's next look; any other cycle is left before its next
        # idle ping, which is no part of the turn.
        while not cycle.done() and clock.now < START + lasts + 30:
            await asyncio.sleep(0.05)
        cycle.cancel()
        await asyncio.gather(cycle, return_exceptions=True)

    asyncio.run(run())
    return sent, [round(t - START) for t in pings], target, seen


def test_a_long_compaction_pings_neither_its_own_body_nor_the_chats(monkeypatch):
    mark = journal.JOURNAL.last_seq
    sent, offsets, target, seen = story(monkeypatch, COMPACTION, SUMMARY_BODY)
    assert (offsets, [body["messages"] for body in sent]) == ([], []), "a ping of a prefix nobody continues"
    assert seen == [{"turn_body": None, "busy": True, "compaction": True}], "no keepwarm body is armed for the summary"
    assert target._warm_blocked == "compacted" and target._warm_body == CHAT_BODY
    assert getattr(target, "_turn_compaction", False) is False, "the mark lives as long as the compaction"
    assert [(stop["reason"], stop["pings"]) for stop in stops(mark)] == [("compacted", 0)]


def test_a_compaction_starts_no_cycle_of_its_own(monkeypatch):
    ensured = []
    monkeypatch.setattr(direct, "_ensure_keepwarm", lambda *args: ensured.append(args))
    scripted(monkeypatch, SUMMARY_BODY)
    target = chat_session()
    asyncio.run(one_turn(target, COMPACTION))
    assert ensured == [] and target._turn_body is None
    assert target._warm_blocked == "compacted" and target._turn_outcome == "content"


def test_an_ordinary_long_turn_is_still_warmed_with_its_own_body(monkeypatch):
    mark = journal.JOURNAL.last_seq
    sent, offsets, target, seen = story(monkeypatch, CHAT, TURN_BODY)
    assert seen == [{"turn_body": TURN_BODY, "busy": True, "compaction": False}]
    assert len(offsets) == 2 and 199 <= offsets[0] <= 202 and 439 <= offsets[1] <= 443
    assert [body["messages"] for body in sent] == [TURN_BODY["messages"]] * 2
    assert target._warm_blocked is None and target._warm_body == TURN_BODY
    pinged = [e["data"] for e in journal.JOURNAL.query(since=mark, limit=1000) if e["kind"] == "cache.keepwarm"]
    assert [event["during_turn"] for event in pinged] == [True, True]


def test_the_chats_next_turn_after_a_compaction_is_warmed_again(monkeypatch):
    scripted(monkeypatch, SUMMARY_BODY)
    target = chat_session()
    ensured = []
    monkeypatch.setattr(direct, "_ensure_keepwarm", lambda *args: ensured.append(args))
    asyncio.run(one_turn(target, COMPACTION))
    scripted(monkeypatch, TURN_BODY)
    asyncio.run(one_turn(target, CHAT))
    assert target._warm_blocked is None and target._warm_body == TURN_BODY
    assert len(ensured) == 2, "the chat's turn arms its scheduler when it begins and again when it ends"
