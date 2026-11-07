"""After a client's compaction keepwarm stops warming the history the client replaced (C3 risk): the summary request is no
warm body, the cycle ends with the reason `compacted`, and the next real turn of the chat starts one again."""
import asyncio
import time


import client_context
import direct
import journal
from keepwarm_harness import CFG, START, drive, session as warm_session, stops
from prompt_support import prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import Chat
from test_keepwarm_turn_outcome import BODY_A, BODY_B, IDENTITY_A, TEXT, scripted, turn_session
from test_native_history import tool_reply

COMPACTION = {"client": "opencode", "request_id": "req-compact", "client_task": client_context.COMPACTION}
CHAT = {"client": "opencode", "request_id": "req-chat"}


def run_turn(session, identity):
    """Consume one turn as the orchestrator does, under the identity the HTTP layer bound for it."""
    async def consume():
        token = client_context.bind(identity)
        agen = direct.iter_events({}, session, {"model": "model-B"}, "", "", None, "auto", 32)
        try:
            async for _event in agen:
                pass
        finally:
            client_context.reset(token)
            await agen.aclose()

    asyncio.run(consume())


# -- the verdict on a compaction turn --------------------------------------------------------------------------------------------
def test_a_compaction_turn_is_not_a_warm_body_and_marks_the_dialog(monkeypatch):
    ensured = scripted(monkeypatch, TEXT)
    session = turn_session()
    run_turn(session, COMPACTION)
    assert session._turn_outcome == "content" and session._warm_blocked == "compacted"
    # the body the chat last warmed stays, the summary request neither replaces it nor moves its touch
    assert session._warm_body is BODY_A and session._warm_identity == IDENTITY_A and session._warm_at == 5.0
    assert len(ensured) == 1, "a scheduler is started with the turn and not again at the end of a compaction"
    assert session._last_cache_key == "key-B"  # the cache itself was written under this key all the same


def test_the_next_real_turn_of_the_chat_is_a_warm_body_again_and_starts_a_cycle(monkeypatch):
    session = turn_session()
    scripted(monkeypatch, TEXT)
    run_turn(session, COMPACTION)
    assert session._warm_blocked == "compacted"
    ensured = scripted(monkeypatch, TEXT)
    run_turn(session, CHAT)
    assert session._warm_blocked is None and session._warm_body is BODY_B and session._warm_identity == CHAT
    assert len(ensured) == 2


def test_a_second_compaction_leaves_the_mark_alone(monkeypatch):
    session = turn_session()
    for _ in range(2):
        scripted(monkeypatch, TEXT)
        run_turn(session, COMPACTION)
        assert session._warm_blocked == "compacted" and session._warm_body is BODY_A


def test_a_compaction_that_gave_no_answer_says_what_any_failed_turn_says(monkeypatch):
    scripted(monkeypatch, [{"type": "end", "reason": "stop"}])
    session = turn_session()
    run_turn(session, COMPACTION)
    assert session._turn_outcome == "empty" and getattr(session, "_warm_blocked", None) is None
    assert session._warm_body is BODY_A


# -- the cycle -----------------------------------------------------------------------------------------------------------------
def test_a_cycle_that_meets_the_mark_stops_at_once_with_its_own_reason(monkeypatch):
    mark = journal.JOURNAL.last_seq
    target = warm_session(_turn_outcome="content", _warm_blocked="compacted", _warm_at=START - 100)
    clock, pings = drive(monkeypatch, target, seconds=3000)
    assert pings == [] and clock.now - START < 2
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"], stop["credits"], stop["level"]) == ("compacted", 0, 0, "info")
    assert direct._STOP_TEXT["compacted"] in [
        e["message"] for e in journal.JOURNAL.query(since=mark, limit=100) if e["kind"] == "cache.keepwarm_stop"]


def test_a_cycle_that_is_running_when_the_compaction_ends_stops_at_the_next_look(monkeypatch):
    mark = journal.JOURNAL.last_seq
    target = warm_session(_turn_outcome="content", _warm_at=START - 100)

    def compaction_ends(now):
        if now > START + 300 and not getattr(target, "_warm_blocked", None):
            target._warm_blocked = "compacted"

    clock, pings = drive(monkeypatch, target, seconds=3000, on_tick=compaction_ends)
    assert len(pings) == 1, "the ping due at 380 s never goes out"
    assert START + 300 < clock.now < START + 302
    [stop] = stops(mark)
    assert (stop["reason"], stop["pings"]) == ("compacted", 1)


def test_the_mark_does_not_stop_the_pings_inside_the_next_turn(monkeypatch):
    """The mark describes the turn that ended; a real turn clears it when it begins, and one in flight is not judged by it."""
    target = warm_session(_warm_blocked="compacted", _busy=True, _turn_started_at=START, _turn_seq=1,
                          _turn_body={"model": "claude-opus-5-5"}, _warm_at=0)
    _, pings = drive(monkeypatch, target, {"turn_timeout": 3000}, seconds=300)
    assert [round(t - START) for t in pings] == [200]


def test_a_lease_of_a_child_does_not_start_a_cycle_for_a_compacted_dialog():
    async def lease(target):
        direct._ensure_keepwarm(target, CFG)
        await asyncio.sleep(0)

    marked = warm_session(_turn_outcome="content", _warm_blocked="compacted", _warm_at=time.time())
    asyncio.run(lease(marked))
    assert getattr(marked, "_warm_task", None) is None
    live = warm_session(_turn_outcome="content", _warm_at=time.time())
    asyncio.run(lease(live))
    assert live._warm_task is not None  # the same dialog without the mark does get its cycle


def test_the_stop_reason_has_a_russian_text_like_the_others():
    assert direct._STOP_TEXT["compacted"].startswith("Прогрев остановлен")


# -- through the relay -----------------------------------------------------------------------------------------------------------
def wait_for(predicate, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    return None


def test_after_a_real_compaction_the_dialog_is_no_longer_warmed_until_its_next_turn(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    chat = Chat(relay, effort="max")
    first, second = chat.work(2)
    mark = relay.mark()
    summary = chat.compact(effort="high")
    stop = wait_for(lambda: [e for e in relay.events(mark, "cache.keepwarm_stop") if e["data"]["reason"] == "compacted"])
    assert stop and stop[0]["message"].endswith(direct._STOP_TEXT["compacted"])  # the journal prefixes the client's label
    session = relay.runtime.pool._conversations["header:ses_c3"]
    # the body kept warm is the chat's last real turn, not the summary request
    assert session._warm_blocked == "compacted" and len(session._warm_body["messages"]) == len(second["messages"])
    assert len(summary["messages"]) > len(second["messages"])
    assert "client_task" not in session._warm_identity
    # the chat goes on: its next turn is a warm body again and the mark is gone
    relay.scripts.append(tool_reply("c9", "opaque-9"))
    chat.ask(tool_reply("c3", "opaque-3"))
    assert session._warm_blocked is None and "client_task" not in session._warm_identity
    assert len(session._warm_body["messages"]) > len(second["messages"])
